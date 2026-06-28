from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator


class Produit(models.Model):
    TAILLE_CHOICES = [
        ('Mini', 'Mini'),
        ('Moyenne', 'Moyenne'),
        ('Grande', 'Grande'),
    ]

    nom = models.CharField(max_length=100)
    stock = models.IntegerField(default=0, help_text='Stock actuel en unités')
    quantite_minimale = models.IntegerField(default=0, help_text='Quantité minimale à maintenir')
    taille = models.CharField(max_length=10, choices=TAILLE_CHOICES, default='Moyenne', null=True, blank=True)
    poids = models.PositiveIntegerField(null=True, blank=True, help_text='Poids en grammes')
    prix_unitaire = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    is_produit_semi_fini = models.BooleanField(default=False)
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Produit'
        verbose_name_plural = 'Produits'
        ordering = ['nom']

    def __str__(self):
        return self.nom


class MatierePremiereFamille(models.Model):
    famille = models.CharField(max_length=50)
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Famille de matière première'
        verbose_name_plural = 'Familles de matières premières'
        ordering = ['famille']

    def __str__(self):
        return self.famille


class MatierePremiere(models.Model):
    UNITE_CHOICES = [
        ('Pièce', 'Pièce'),
        ('Kg', 'Kg'),
        ('Litre', 'Litre'),
    ]

    matiere_premiere_famille = models.ForeignKey(
        MatierePremiereFamille,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='matieres_premieres',
    )
    nom = models.CharField(max_length=100)
    unite = models.CharField(max_length=10, choices=UNITE_CHOICES, default='Kg', null=True, blank=True)
    quantite = models.DecimalField(
        max_digits=11, decimal_places=3,
        null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    stock_minimum = models.DecimalField(
        max_digits=11, decimal_places=3,
        null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    # Relation circulaire volontaire : matière première peut être un produit semi-fini
    produit = models.ForeignKey(
        Produit,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='matieres_premieres_semi_fini',
        help_text='Renseigner uniquement si cette matière est un produit semi-fini',
    )

    class Meta:
        verbose_name = 'Matière première'
        verbose_name_plural = 'Matières premières'
        ordering = ['nom']

    def __str__(self):
        return f'{self.nom} ({self.unite})'


class Recette(models.Model):
    produit = models.ForeignKey(
        Produit,
        on_delete=models.RESTRICT,
        related_name='recettes',
    )
    quantite = models.DecimalField(
        max_digits=8, decimal_places=3,
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Quantité produite par cette recette',
    )
    observation = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = 'Recette'
        verbose_name_plural = 'Recettes'

    def __str__(self):
        return f'Recette — {self.produit} (×{self.quantite})'


class RecetteDetaille(models.Model):
    recette = models.ForeignKey(
        Recette,
        on_delete=models.CASCADE,
        related_name='details',
    )
    matiere_premiere = models.ForeignKey(
        MatierePremiere,
        on_delete=models.RESTRICT,
        related_name='recette_details',
    )
    quantite = models.DecimalField(
        max_digits=8, decimal_places=3,
        null=True, blank=True,
        validators=[MinValueValidator(0)],
    )

    class Meta:
        verbose_name = 'Détail de recette'
        verbose_name_plural = 'Détails de recettes'

    def __str__(self):
        return f'{self.recette} — {self.matiere_premiere} ({self.quantite})'


class CommandeInterne(models.Model):
    produit = models.ForeignKey(
        Produit,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='commandes_internes',
    )
    livraison_at = models.DateField(null=True, blank=True)
    quantite_commander = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    quantite_produite = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    is_stock_maj = models.BooleanField(default=False, help_text='Indique si le stock a été mis à jour')

    class Meta:
        verbose_name = 'Commande interne'
        verbose_name_plural = 'Commandes internes'
        ordering = ['-livraison_at']

    def __str__(self):
        return f'CI#{self.id} — {self.produit} ({self.livraison_at})'


class HistoriqueStockInitial(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='historiques_stock_initial',
    )
    stock_initial_at = models.DateField()

    class Meta:
        verbose_name = 'Historique stock initial'
        verbose_name_plural = 'Historiques stock initial'
        ordering = ['-stock_initial_at']

    def __str__(self):
        return f'Stock initial du {self.stock_initial_at} — {self.user}'


class HistoriqueStockInitialDetaille(models.Model):
    historique_stock_initial = models.ForeignKey(
        HistoriqueStockInitial,
        on_delete=models.RESTRICT,
        related_name='details',
    )
    produit = models.ForeignKey(
        Produit,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='historiques_stock',
    )
    calcul_stock = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Stock calculé système')
    reel_stock = models.PositiveSmallIntegerField(null=True, blank=True, help_text='Stock réel constaté')
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Détail historique stock initial'
        verbose_name_plural = 'Détails historique stock initial'

    def __str__(self):
        return f'{self.historique_stock_initial} — {self.produit}'


class BonRestitution(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='bons_restitution',
    )
    restitution_at = models.DateField()
    produit = models.ForeignKey(
        Produit,
        on_delete=models.RESTRICT,
        related_name='bons_restitution',
    )
    quantite = models.DecimalField(
        max_digits=8, decimal_places=0,
        default=0,
        validators=[MinValueValidator(0)],
    )
    hist_stock_initial = models.ForeignKey(
        HistoriqueStockInitial,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='bons_restitution',
    )

    class Meta:
        verbose_name = 'Bon de restitution'
        verbose_name_plural = 'Bons de restitution'
        ordering = ['-restitution_at']

    def __str__(self):
        return f'BR#{self.id} — {self.produit} ({self.restitution_at})'


class BonSortie(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='bons_sortie',
    )
    sortie_at = models.DateField(null=True, blank=True)
    produit = models.ForeignKey(
        Produit,
        on_delete=models.RESTRICT,
        related_name='bons_sortie',
    )
    quantite = models.DecimalField(
        max_digits=8, decimal_places=0,
        default=0,
        validators=[MinValueValidator(0)],
    )
    hist_stock_initial = models.ForeignKey(
        HistoriqueStockInitial,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='bons_sortie',
    )

    class Meta:
        verbose_name = 'Bon de sortie'
        verbose_name_plural = 'Bons de sortie'
        ordering = ['-sortie_at']

    def __str__(self):
        return f'BS#{self.id} — {self.produit} ({self.sortie_at})'
