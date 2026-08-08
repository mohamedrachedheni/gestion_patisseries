"""Exécution réelle des sauvegardes/restaurations de la base de données —
mysqldump/mysql (client MariaDB/MySQL), chiffrement optionnel des fichiers au
repos via Fernet (cryptography).

Séparé de views.py : cette logique touche à des sous-processus système, des
fichiers sensibles et des identifiants de connexion — elle mérite d'être
isolée et testée indépendamment de la couche HTTP.

Jamais de mot de passe passé en argument de ligne de commande (visible dans
la liste des processus) : toujours via un fichier --defaults-extra-file
temporaire, supprimé immédiatement après l'exécution.
"""
import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

# Dossiers usuels où chercher mysqldump/mysql si absents du PATH (ex: XAMPP
# sur Windows, qui n'ajoute jamais son MySQL au PATH système par défaut).
_FALLBACK_DIRS = [
    r'C:\xampp\mysql\bin',
    r'C:\Program Files\MySQL\MySQL Server 8.0\bin',
    r'C:\Program Files\MySQL\MySQL Server 8.4\bin',
    r'C:\wamp64\bin\mysql',
]


@dataclass
class Resultat:
    ok: bool
    erreur: str = ''


def _resoudre_executable(nom):
    trouve = shutil.which(nom)
    if trouve:
        return trouve
    for dossier in _FALLBACK_DIRS:
        candidat = os.path.join(dossier, f'{nom}.exe')
        if os.path.isfile(candidat):
            return candidat
        # wamp64 imbrique le binaire dans un sous-dossier versionné (mysqlX.Y.Z/bin)
        if os.path.isdir(dossier):
            for sous in os.listdir(dossier):
                candidat = os.path.join(dossier, sous, 'bin', f'{nom}.exe')
                if os.path.isfile(candidat):
                    return candidat
    return None


def _ecrire_fichier_defaults(db_config):
    """Fichier --defaults-extra-file temporaire (identifiants jamais en argument
    de ligne de commande). Valeurs entre guillemets pour neutraliser tout
    caractère spécial du mot de passe (ex: '#', traité comme un début de
    commentaire par le parseur d'options MySQL en dehors de guillemets)."""
    fd, chemin = tempfile.mkstemp(prefix='dbcreds_', suffix='.cnf')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write('[client]\n')
        f.write(f'user="{db_config["USER"]}"\n')
        f.write(f'password="{db_config["PASSWORD"]}"\n')
        f.write(f'host="{db_config["HOST"]}"\n')
        f.write(f'port={db_config["PORT"]}\n')
    try:
        os.chmod(chemin, 0o600)
    except OSError:
        pass
    return chemin


def _fernet():
    """Clé de chiffrement dérivée de SECRET_KEY — aucun secret additionnel à
    gérer, mais implique que les sauvegardes chiffrées deviennent illisibles
    si SECRET_KEY change un jour (à documenter/anticiper avant toute rotation)."""
    cle = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest())
    return Fernet(cle)


def _chemin_absolu(chemin_relatif):
    return os.path.join(settings.DB_BACKUPS_DIR, chemin_relatif)


def executer_sauvegarde(sauvegarde):
    """Exécute mysqldump pour `sauvegarde` (déjà créée en base, reussie=False)
    et met à jour ses champs (reussie/taille/chemin_fichier/message_erreur)
    en conséquence. Retourne un Resultat."""
    db = settings.DATABASES['default']
    mysqldump = _resoudre_executable('mysqldump')
    if mysqldump is None:
        sauvegarde.reussie = False
        sauvegarde.message_erreur = "Exécutable 'mysqldump' introuvable sur ce serveur."
        sauvegarde.save(update_fields=['reussie', 'message_erreur'])
        return Resultat(False, sauvegarde.message_erreur)

    horodatage = timezone.now().strftime('%Y%m%d_%H%M%S')
    nom_fichier = f'sauvegarde_{sauvegarde.pk}_{horodatage}.sql'
    chemin_sql = _chemin_absolu(nom_fichier)

    commande = [mysqldump, '--result-file=' + chemin_sql]
    if sauvegarde.type_sauvegarde == 'Structure seule':
        commande.append('--no-data')
    elif sauvegarde.type_sauvegarde == 'Données seules':
        commande.append('--no-create-info')
    commande.append(db['NAME'])

    defaults_file = _ecrire_fichier_defaults(db)
    try:
        commande.insert(1, f'--defaults-extra-file={defaults_file}')
        processus = subprocess.run(commande, capture_output=True, text=True, timeout=600)
    except Exception as exc:
        sauvegarde.reussie = False
        sauvegarde.message_erreur = f"Échec du lancement de mysqldump : {exc}"
        sauvegarde.save(update_fields=['reussie', 'message_erreur'])
        return Resultat(False, sauvegarde.message_erreur)
    finally:
        os.remove(defaults_file)

    if processus.returncode != 0:
        if os.path.isfile(chemin_sql):
            os.remove(chemin_sql)
        sauvegarde.reussie = False
        sauvegarde.message_erreur = processus.stderr.strip() or 'Échec de mysqldump (code de sortie non nul).'
        sauvegarde.save(update_fields=['reussie', 'message_erreur'])
        return Resultat(False, sauvegarde.message_erreur)

    if sauvegarde.chiffre:
        with open(chemin_sql, 'rb') as f:
            contenu_clair = f.read()
        contenu_chiffre = _fernet().encrypt(contenu_clair)
        nom_fichier_final = nom_fichier + '.enc'
        with open(_chemin_absolu(nom_fichier_final), 'wb') as f:
            f.write(contenu_chiffre)
        os.remove(chemin_sql)
    else:
        nom_fichier_final = nom_fichier

    chemin_final = _chemin_absolu(nom_fichier_final)
    sauvegarde.chemin_fichier = nom_fichier_final
    sauvegarde.taille = os.path.getsize(chemin_final)
    sauvegarde.reussie = True
    sauvegarde.message_erreur = None
    sauvegarde.save(update_fields=['chemin_fichier', 'taille', 'reussie', 'message_erreur'])
    return Resultat(True)


def lire_contenu_clair(sauvegarde):
    """Retourne le contenu SQL en clair (bytes) de `sauvegarde`, en
    déchiffrant à la volée si nécessaire — jamais de fichier déchiffré
    persisté sur disque."""
    chemin = _chemin_absolu(sauvegarde.chemin_fichier)
    with open(chemin, 'rb') as f:
        contenu = f.read()
    if not sauvegarde.chiffre:
        return contenu
    try:
        return _fernet().decrypt(contenu)
    except InvalidToken:
        raise ValueError('Impossible de déchiffrer ce fichier (clé de chiffrement incompatible).')


def executer_restauration(sauvegarde):
    """Restaure la base de données à partir de `sauvegarde` (remplace TOUT le
    contenu des tables concernées par le dump — opération destructive et
    irréversible sur l'état courant). L'appelant (vue) est responsable de
    déclencher une sauvegarde de sécurité AVANT d'appeler cette fonction."""
    db = settings.DATABASES['default']
    mysql_bin = _resoudre_executable('mysql')
    if mysql_bin is None:
        return Resultat(False, "Exécutable 'mysql' introuvable sur ce serveur.")

    try:
        contenu_clair = lire_contenu_clair(sauvegarde)
    except (OSError, ValueError) as exc:
        return Resultat(False, str(exc))

    defaults_file = _ecrire_fichier_defaults(db)
    try:
        processus = subprocess.run(
            [mysql_bin, f'--defaults-extra-file={defaults_file}', db['NAME']],
            input=contenu_clair, capture_output=True, timeout=600,
        )
    except Exception as exc:
        return Resultat(False, f"Échec du lancement de mysql : {exc}")
    finally:
        os.remove(defaults_file)

    if processus.returncode != 0:
        erreur = processus.stderr.decode('utf-8', errors='replace').strip()
        return Resultat(False, erreur or 'Échec de la restauration (code de sortie non nul).')
    return Resultat(True)
