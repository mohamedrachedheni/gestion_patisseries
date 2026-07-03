from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.utils import timezone


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


class LibelleTransaction(models.Model):
    libelle = models.CharField(max_length=50, unique=True)
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Libellé transaction'
        verbose_name_plural = 'Libellés transaction'
        ordering = ['libelle']

    def __str__(self):
        return self.libelle


class Transaction(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='transactions',
    )
    # Pas auto_now_add : un transfert backdaté doit pouvoir imposer sa propre
    # date d'effet (transfere_at) aux deux transactions qu'il génère.
    created_at = models.DateTimeField(default=timezone.now)
    libelle = models.ForeignKey(
        LibelleTransaction,
        on_delete=models.RESTRICT,
        related_name='transactions',
    )
    table_id = models.CharField(
        max_length=50,
        unique=True,
        help_text='Référence unique au format : nom_table_id_123',
    )
    montant = models.DecimalField(
        max_digits=11, decimal_places=3,
        help_text='Crédit (+) ou débit (-) en caisse',
    )
    hist_solde_compte = models.ForeignKey(
        HistoriqueSoldeCompte,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='transactions',
        help_text='Renseigné une fois la transaction intégrée à un solde : sert de statut « Soldé »',
    )

    class Meta:
        verbose_name = 'Transaction caisse'
        verbose_name_plural = 'Transactions caisse'
        ordering = ['-created_at']

    def __str__(self):
        signe = '+' if self.montant >= 0 else '-'
        return f'{self.libelle}  {signe}{self.montant} TND ({self.user})'


# Libellés système utilisés pour les deux transactions générées par un transfert.
# Références en dur (et non par ID) : voir Transfere.creer_transactions().
LIBELLE_TRANSFERT_RECU = 'Transfère montant reçu'
LIBELLE_TRANSFERT_VERSE = 'Transfère montant versé'


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
    montant = models.DecimalField(max_digits=8, decimal_places=3, default=0)

    class Meta:
        verbose_name = 'Transfert'
        verbose_name_plural = 'Transferts'
        ordering = ['-transfere_at']
        constraints = [
            models.CheckConstraint(
                check=models.Q(montant__gt=0),
                name='transfere_montant_positif',
            ),
            models.CheckConstraint(
                check=~models.Q(compte_source=models.F('compte_destination')),
                name='transfere_comptes_differents',
            ),
        ]

    def __str__(self):
        return f'{self.compte_source} → {self.compte_destination} : {self.montant} TND ({self.transfere_at})'

    def clean(self):
        super().clean()
        errors = {}
        if self.montant is not None and self.montant <= 0:
            errors['montant'] = 'Le montant doit être strictement positif.'
        if (
            self.compte_source_id and self.compte_destination_id
            and self.compte_source_id == self.compte_destination_id
        ):
            errors['compte_destination'] = 'Le compte destination doit être différent du compte source.'
        if errors:
            raise ValidationError(errors)

    def creer_transactions(self):
        """Crée les deux transactions caisse (reçue / versée) reflétant ce transfert.
        À appeler à l'intérieur d'une transaction.atomic() avec la sauvegarde du transfert."""
        libelle_recu = LibelleTransaction.objects.get_or_create(libelle=LIBELLE_TRANSFERT_RECU)[0]
        libelle_verse = LibelleTransaction.objects.get_or_create(libelle=LIBELLE_TRANSFERT_VERSE)[0]
        Transaction.objects.create(
            user=self.compte_destination,
            created_at=self.transfere_at,
            libelle=libelle_recu,
            table_id=f'transfere_recu_id_{self.pk}',
            montant=self.montant,
        )
        Transaction.objects.create(
            user=self.compte_source,
            created_at=self.transfere_at,
            libelle=libelle_verse,
            table_id=f'transfere_verse_id_{self.pk}',
            montant=-self.montant,
        )

    def get_transactions_liees(self):
        """Retourne (transaction_recue, transaction_versee). Lève Transaction.DoesNotExist
        si l'une des deux est absente — appelant : traiter comme une incohérence."""
        return (
            Transaction.objects.get(table_id=f'transfere_recu_id_{self.pk}'),
            Transaction.objects.get(table_id=f'transfere_verse_id_{self.pk}'),
        )

    def a_transactions_soldees(self):
        """True si l'une des transactions liées (parmi celles qui existent) a déjà
        été intégrée à un calcul de solde (hist_solde_compte renseigné) — dans ce
        cas le transfert ne doit plus pouvoir être modifié ni supprimé."""
        return Transaction.objects.filter(
            table_id__in=[f'transfere_recu_id_{self.pk}', f'transfere_verse_id_{self.pk}'],
            hist_solde_compte__isnull=False,
        ).exists()

    def modifier_transactions_liees(self):
        """Répercute les valeurs courantes de ce transfert sur ses deux transactions
        caisse existantes. Lève Transaction.DoesNotExist si l'une des deux manque —
        à appeler avant toute sauvegarde, à l'intérieur d'une transaction.atomic()."""
        transaction_recue, transaction_versee = self.get_transactions_liees()
        transaction_recue.user = self.compte_destination
        transaction_recue.created_at = self.transfere_at
        transaction_recue.montant = self.montant
        transaction_recue.save(update_fields=['user', 'created_at', 'montant'])
        transaction_versee.user = self.compte_source
        transaction_versee.created_at = self.transfere_at
        transaction_versee.montant = -self.montant
        transaction_versee.save(update_fields=['user', 'created_at', 'montant'])

    def supprimer_transactions_liees(self):
        """Supprime les transactions caisse liées à ce transfert, si elles existent."""
        Transaction.objects.filter(
            table_id__in=[f'transfere_recu_id_{self.pk}', f'transfere_verse_id_{self.pk}'],
        ).delete()
