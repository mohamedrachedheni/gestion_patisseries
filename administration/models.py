from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator


class Employe(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='employes',
    )
    matricule = models.CharField(max_length=50, null=True, blank=True)
    service = models.CharField(max_length=100, null=True, blank=True)
    naissance_at = models.DateField(null=True, blank=True, verbose_name='Date de naissance')
    embauche_at = models.DateField(null=True, blank=True, verbose_name="Date d'embauche")
    type_contrat = models.CharField(max_length=50, null=True, blank=True)
    telephone = models.CharField(max_length=30, null=True, blank=True)
    adresse = models.CharField(max_length=255, null=True, blank=True)
    photo = models.ImageField(upload_to='employes/', null=True, blank=True)
    salaire = models.DecimalField(
        max_digits=7, decimal_places=3,
        null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    observation = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = 'Employé'
        verbose_name_plural = 'Employés'
        ordering = ['user__last_name', 'user__first_name']

    def __str__(self):
        return f'{self.user.get_full_name()} ({self.matricule or "—"})'


class HistoriqueSoldeCompte(models.Model):
    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='soldes_administres',
        help_text='Administrateur ayant effectué la correction',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='historiques_solde',
        help_text='Commercial concerné par ce solde',
    )
    solde_at = models.DateField()
    calcul_solde = models.DecimalField(max_digits=11, decimal_places=3, default=0, help_text='Solde calculé automatiquement')
    correction_solde = models.DecimalField(max_digits=11, decimal_places=3, default=0, help_text='Correction manuelle appliquée')
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Historique solde compte'
        verbose_name_plural = 'Historiques soldes compte'
        ordering = ['-solde_at']

    def __str__(self):
        return f'Solde {self.user} au {self.solde_at}'


class Transaction(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='transactions',
    )
    created_at = models.DateTimeField(null=True, blank=True)
    operation = models.CharField(max_length=100)
    table_id = models.CharField(
        max_length=50,
        unique=True,
        help_text='Référence unique au format : nom_table_id_123',
    )
    montant = models.DecimalField(
        max_digits=11, decimal_places=3,
        help_text='Crédit (+) ou débit (-) en caisse',
    )

    class Meta:
        verbose_name = 'Transaction caisse'
        verbose_name_plural = 'Transactions caisse'
        ordering = ['-created_at']

    def __str__(self):
        signe = '+' if self.montant >= 0 else ''
        return f'{self.operation} — {signe}{self.montant} TND ({self.user})'


class Transfere(models.Model):
    transfere_at = models.DateField(null=True, blank=True)
    compte_source = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='transferts_emis',
    )
    libelle = models.CharField(max_length=50, null=True, blank=True)
    compte_destination = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='transferts_recus',
    )
    montant = models.DecimalField(max_digits=11, decimal_places=3, default=0)

    class Meta:
        verbose_name = 'Transfert'
        verbose_name_plural = 'Transferts'
        ordering = ['-transfere_at']

    def __str__(self):
        return f'{self.compte_source} → {self.compte_destination} : {self.montant} TND ({self.transfere_at})'
