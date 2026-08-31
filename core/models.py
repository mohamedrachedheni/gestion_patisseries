from django.conf import settings
from django.db import models


class VerrouillageConnexion(models.Model):
    """Compteur d'échecs de connexion par identifiant saisi sur /login/ —
    protection anti-brute-force. Voir core/services.py pour la logique
    (verrouillage temporaire au bout de N échecs)."""
    identifiant = models.CharField(max_length=150, unique=True)
    echecs = models.PositiveSmallIntegerField(default=0)
    verrouille_jusqu_a = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Verrouillage de connexion'
        verbose_name_plural = 'Verrouillages de connexion'

    def __str__(self):
        return self.identifiant


class CodeConnexion(models.Model):
    """Code de vérification à 6 chiffres envoyé par email à la 2ᵉ étape de la
    connexion (mot de passe déjà validé, session pas encore ouverte). Voir
    core/services.py et core/views.py::LoginCodeView."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='codes_connexion',
    )
    code = models.CharField(max_length=6)
    cree_le = models.DateTimeField(auto_now_add=True)
    expire_le = models.DateTimeField()
    tentatives = models.PositiveSmallIntegerField(default=0)
    utilise = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'Code de connexion'
        verbose_name_plural = 'Codes de connexion'
        ordering = ['-cree_le']

    def __str__(self):
        return f'Code pour {self.user} ({"utilisé" if self.utilise else "en attente"})'


class JourNonOuvre(models.Model):
    TYPE_CHOICES = [
        ('Férié national', 'Férié national'),
        ('Fermeture entreprise', 'Fermeture entreprise'),
        ('Congé exceptionnel', 'Congé exceptionnel'),
    ]

    date = models.DateField(unique=True)
    libelle = models.CharField(max_length=150)
    type_jour = models.CharField(max_length=30, choices=TYPE_CHOICES, default='Férié national')
    concerne_livraison = models.BooleanField(
        default=True,
        help_text="Décoché si ce jour férié n'empêche pas réellement les livraisons",
    )

    class Meta:
        verbose_name = 'Jour non ouvré'
        verbose_name_plural = 'Jours non ouvrés'
        ordering = ['date']

    def __str__(self):
        return f'{self.libelle} ({self.date})'
