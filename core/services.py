"""Sécurité de la connexion (/login/) :
- verrouillage anti-brute-force par identifiant après échecs répétés ;
- code de vérification à 6 chiffres envoyé par email (2ᵉ facteur), déclenché
  une fois l'identifiant + mot de passe validés.

Utilisé par core/views.py (CustomLoginView, LoginCodeView).
"""
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import CodeConnexion, VerrouillageConnexion

MAX_ECHECS = 6
DUREE_VERROUILLAGE = timedelta(minutes=1)
DUREE_VALIDITE_CODE = timedelta(minutes=5)
MAX_TENTATIVES_CODE = 5
DELAI_MIN_RENVOI = timedelta(seconds=30)


def normaliser_identifiant(identifiant):
    return (identifiant or '').strip().lower()


def est_verrouille(identifiant):
    """Retourne la date de fin de verrouillage si l'identifiant est
    actuellement verrouillé, sinon None."""
    identifiant = normaliser_identifiant(identifiant)
    if not identifiant:
        return None
    verrou = VerrouillageConnexion.objects.filter(identifiant=identifiant).first()
    if verrou and verrou.verrouille_jusqu_a and verrou.verrouille_jusqu_a > timezone.now():
        return verrou.verrouille_jusqu_a
    return None


def enregistrer_echec(identifiant):
    """Incrémente le compteur d'échecs de l'identifiant ; le verrouille
    `DUREE_VERROUILLAGE` dès que `MAX_ECHECS` échecs sont atteints."""
    identifiant = normaliser_identifiant(identifiant)
    if not identifiant:
        return
    verrou, _ = VerrouillageConnexion.objects.get_or_create(identifiant=identifiant)
    verrou.echecs += 1
    if verrou.echecs >= MAX_ECHECS:
        verrou.verrouille_jusqu_a = timezone.now() + DUREE_VERROUILLAGE
        verrou.echecs = 0
    verrou.save()


def reinitialiser_echecs(identifiant):
    VerrouillageConnexion.objects.filter(identifiant=normaliser_identifiant(identifiant)).delete()


def generer_code_connexion(user):
    """Crée un nouveau code à 6 chiffres pour `user` et invalide les codes
    précédents non utilisés (un seul code actif à la fois)."""
    CodeConnexion.objects.filter(user=user, utilise=False).update(utilise=True)
    code = f'{secrets.randbelow(1_000_000):06d}'
    return CodeConnexion.objects.create(
        user=user,
        code=code,
        expire_le=timezone.now() + DUREE_VALIDITE_CODE,
    )


def masquer_email(email):
    """« jean.dupont@example.com » → « j***@example.com » — pour l'affichage
    à l'écran sans révéler l'adresse complète."""
    local, _, domaine = (email or '').partition('@')
    if not domaine:
        return email
    return f'{local[:1]}***@{domaine}'


def envoyer_code_connexion(user, code_connexion):
    minutes = int(DUREE_VALIDITE_CODE.total_seconds() // 60)
    send_mail(
        subject='Votre code de connexion — Gestion Pâtisseries',
        message=(
            f'Bonjour {user.first_name or user.get_username()},\n\n'
            f'Votre code de connexion est : {code_connexion.code}\n'
            f'Il est valable {minutes} minutes.\n\n'
            "Si vous n'êtes pas à l'origine de cette tentative de connexion, "
            'contactez un administrateur.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
