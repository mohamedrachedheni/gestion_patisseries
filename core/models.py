from django.db import models


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
