from django.db import models

TYPE_SAUVEGARDE_CHOICES = [
    ('Complète', 'Complète'),
    ('Structure seule', 'Structure seule'),
    ('Données seules', 'Données seules'),
]


class Sauvegarde(models.Model):
    """Une exécution de sauvegarde de la base de données (réelle — dump via
    mysqldump, voir backups/services.py). `chemin_fichier` est un chemin
    RELATIF à settings.DB_BACKUPS_DIR (jamais un chemin absolu stocké en
    base, pour rester portable si le dossier de stockage est déplacé).

    Vit dans la base 'sauvegardes' (SQLite séparée, voir backups/db_router.py)
    — jamais dans 'default' (MySQL), qui peut être restaurée. `cree_par` est
    donc un simple texte dénormalisé (nom de l'utilisateur) et non une
    ForeignKey vers User : Django ne supporte pas les relations entre deux
    bases physiquement distinctes."""
    nom = models.CharField(max_length=255)
    type_sauvegarde = models.CharField(max_length=20, choices=TYPE_SAUVEGARDE_CHOICES, default='Complète')
    chiffre = models.BooleanField('Chiffré', default=True)
    taille = models.BigIntegerField('Taille (octets)', null=True, blank=True)
    created_at = models.DateTimeField('Date de création', auto_now_add=True)
    reussie = models.BooleanField('Réussie', default=False)
    observation = models.CharField(max_length=255, null=True, blank=True)
    chemin_fichier = models.CharField(max_length=500, null=True, blank=True)
    message_erreur = models.TextField(null=True, blank=True)
    cree_par = models.CharField(max_length=150, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Sauvegarde'
        verbose_name_plural = 'Sauvegardes'

    def __str__(self):
        return self.nom
