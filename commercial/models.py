from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator


MODE_PAIEMENT_CHOICES = [
    ('Espèces', 'Espèces'),
    ('Virement', 'Virement'),
    ('Chèque', 'Chèque'),
    ('Traite', 'Traite'),
]

STATUT_PAIEMENT_CHOICES = [
    ('Non payé', 'Non payé'),
    ('Partiellement payé', 'Partiellement payé'),
    ('Payé', 'Payé'),
]


class Gouvernorat(models.Model):
    nom = models.CharField(max_length=100)

    class Meta:
        verbose_name = 'Gouvernorat'
        verbose_name_plural = 'Gouvernorats'
        ordering = ['nom']

    def __str__(self):
        return self.nom


class Delegation(models.Model):
    gouvernorat = models.ForeignKey(
        Gouvernorat,
        on_delete=models.RESTRICT,
        related_name='delegations',
    )
    nom_delegation = models.CharField(max_length=100)

    class Meta:
        verbose_name = 'Délégation'
        verbose_name_plural = 'Délégations'
        ordering = ['gouvernorat__nom', 'nom_delegation']

    def __str__(self):
        return f'{self.nom_delegation} ({self.gouvernorat})'


class Zone(models.Model):
    delegation = models.ForeignKey(
        Delegation,
        on_delete=models.RESTRICT,
        related_name='zones',
    )
    nom = models.CharField(max_length=100)

    class Meta:
        verbose_name = 'Zone'
        verbose_name_plural = 'Zones'
        ordering = ['delegation__nom_delegation', 'nom']

    def __str__(self):
        return f'{self.nom} — {self.delegation}'


class Fournisseur(models.Model):
    TYPE_CHOICES = [
        ('Matières premières', 'Matières premières'),
        ('Emballages', 'Emballages'),
        ('Matières premières et Emballages', 'Matières premières et Emballages'),
        ('Équipements', 'Équipements'),
        ('Divers', 'Divers'),
    ]

    raison_sociale = models.CharField(max_length=200)
    nom_contact = models.CharField(max_length=150, null=True, blank=True)
    type_fournisseur = models.CharField(
        max_length=50,
        choices=TYPE_CHOICES,
        default='Matières premières et Emballages',
        null=True, blank=True,
    )
    adresse = models.CharField(max_length=255, null=True, blank=True)
    telephone = models.CharField(max_length=255, null=True, blank=True)
    email = models.EmailField(max_length=150, null=True, blank=True)
    tva = models.CharField(max_length=50, null=True, blank=True, verbose_name='N° TVA')
    rib = models.CharField(max_length=100, null=True, blank=True, verbose_name='RIB')
    observations = models.TextField(null=True, blank=True)
    created_at = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'Fournisseur'
        verbose_name_plural = 'Fournisseurs'
        ordering = ['raison_sociale']

    def __str__(self):
        return self.raison_sociale


class Client(models.Model):
    raison_sociale = models.CharField(max_length=200, null=True, blank=True)
    nom_client = models.CharField(max_length=150, null=True, blank=True)
    adresse = models.CharField(max_length=255, null=True, blank=True)
    zone = models.ForeignKey(
        Zone,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='clients',
    )
    telephone = models.CharField(max_length=255, null=True, blank=True)
    photo = models.ImageField(upload_to='clients/', null=True, blank=True)
    google_mape = models.TextField(null=True, blank=True, verbose_name='Lien Google Maps')
    a_visiter_apres = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Nombre de jours avant la prochaine visite',
    )
    observation = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateField(null=True, blank=True)
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through='ClientUser',
        related_name='clients',
        blank=True,
    )

    class Meta:
        verbose_name = 'Client'
        verbose_name_plural = 'Clients'
        ordering = ['nom_client']

    def __str__(self):
        return self.raison_sociale or self.nom_client or f'Client #{self.pk}'


class ClientUser(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.RESTRICT,
        related_name='client_users',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='client_users',
    )

    class Meta:
        verbose_name = 'Affectation client-commercial'
        verbose_name_plural = 'Affectations client-commerciaux'

    def __str__(self):
        return f'{self.client} → {self.user}'


class Agenda(models.Model):
    STATUS_CHOICES = [
        ('En attente', 'En attente'),
        ('En cours', 'En cours'),
        ('Réaliser', 'Réaliser'),
        ('Annuler', 'Annuler'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='agendas',
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='agendas',
    )
    detaille_action_planifier = models.CharField(max_length=255, null=True, blank=True)
    echeance_at = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=15,
        choices=STATUS_CHOICES,
        default='En attente',
        null=True, blank=True,
    )

    class Meta:
        verbose_name = 'Agenda'
        verbose_name_plural = 'Agendas'
        ordering = ['echeance_at']

    def __str__(self):
        return f'{self.client} — {self.echeance_at} ({self.status})'


class VenteCode(models.Model):
    client = models.ForeignKey(
        Client,
        on_delete=models.RESTRICT,
        related_name='ventes_code',
    )
    vente_at = models.DateField()
    vente_numero = models.CharField(max_length=25)

    class Meta:
        verbose_name = 'Code de vente'
        verbose_name_plural = 'Codes de vente'
        ordering = ['-vente_at', 'vente_numero']
        constraints = [
            models.UniqueConstraint(
                fields=['vente_at', 'vente_numero'],
                name='unique_vente_code',
            ),
        ]

    def __str__(self):
        return f'V-{self.vente_numero} ({self.vente_at})'


class Vente(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='ventes',
    )
    vente_code = models.ForeignKey(
        VenteCode,
        on_delete=models.RESTRICT,
        related_name='ventes',
    )
    total_montant = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    total_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    nouveau_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    reste_a_payer = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    statut = models.CharField(
        max_length=20,
        choices=STATUT_PAIEMENT_CHOICES,
        default='Payé',
        null=True, blank=True,
    )
    mode_paiement = models.CharField(
        max_length=10,
        choices=MODE_PAIEMENT_CHOICES,
        default='Espèces',
        null=True, blank=True,
    )
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Vente'
        verbose_name_plural = 'Ventes'
        ordering = ['-vente_code__vente_at']

    def __str__(self):
        return f'Vente {self.vente_code} — {self.total_montant} TND'


class BonLivraisonCode(models.Model):
    bon_livraison_at = models.DateField()
    bon_livraison_numero = models.SmallIntegerField()
    client = models.ForeignKey(
        Client,
        on_delete=models.RESTRICT,
        related_name='bons_livraison_code',
    )
    vente_code = models.ForeignKey(
        VenteCode,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='bons_livraison_code',
    )
    hist_stock_initial = models.ForeignKey(
        'production.HistoriqueStockInitial',
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='bons_livraison_code',
    )

    class Meta:
        verbose_name = 'Code de bon de livraison'
        verbose_name_plural = 'Codes de bons de livraison'
        ordering = ['-bon_livraison_at', 'bon_livraison_numero']
        constraints = [
            models.UniqueConstraint(
                fields=['bon_livraison_at', 'bon_livraison_numero'],
                name='unique_bon_livraison_couple',
            ),
        ]

    def __str__(self):
        return f'BL-{self.bon_livraison_numero} ({self.bon_livraison_at})'


class BonLivraison(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='bons_livraison',
    )
    bon_livraison_code = models.ForeignKey(
        BonLivraisonCode,
        on_delete=models.RESTRICT,
        related_name='bons_livraison',
    )
    total_montant = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    total_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    nouveau_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    reste_a_payer = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    mode_paiement = models.CharField(max_length=10, choices=MODE_PAIEMENT_CHOICES)
    statut = models.CharField(max_length=20, choices=STATUT_PAIEMENT_CHOICES, default='Non payé')
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Bon de livraison'
        verbose_name_plural = 'Bons de livraison'

    def __str__(self):
        return f'{self.bon_livraison_code} — {self.statut}'


class BonLivraisonDetaille(models.Model):
    bon_livraison = models.ForeignKey(
        BonLivraison,
        on_delete=models.RESTRICT,
        related_name='details',
    )
    produit = models.ForeignKey(
        'production.Produit',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='bons_livraison_details',
    )
    quantite = models.DecimalField(max_digits=8, decimal_places=0, default=0, validators=[MinValueValidator(0)])
    prix_unitaire = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    total_ligne = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])

    class Meta:
        verbose_name = 'Détail de bon de livraison'
        verbose_name_plural = 'Détails de bons de livraison'

    def __str__(self):
        return f'{self.bon_livraison} — {self.produit} (×{self.quantite})'


class VenteDetaille(models.Model):
    vente = models.ForeignKey(
        Vente,
        on_delete=models.CASCADE,
        related_name='details',
    )
    # UNIQUE sur bon_livraison_code_id : un BL ne peut être facturé qu'une seule fois
    bon_livraison_code = models.OneToOneField(
        BonLivraisonCode,
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='vente_detaille',
    )

    class Meta:
        verbose_name = 'Détail de vente'
        verbose_name_plural = 'Détails de ventes'

    def __str__(self):
        return f'{self.vente} — {self.bon_livraison_code}'


class AchatCode(models.Model):
    achat_at = models.DateField()
    achat_numero = models.SmallIntegerField()
    fournisseur = models.ForeignKey(
        Fournisseur,
        on_delete=models.RESTRICT,
        related_name='achats_code',
    )

    class Meta:
        verbose_name = "Code d'achat"
        verbose_name_plural = "Codes d'achats"
        ordering = ['-achat_at', 'achat_numero']
        constraints = [
            models.UniqueConstraint(
                fields=['achat_at', 'achat_numero'],
                name='unique_achat',
            ),
        ]

    def __str__(self):
        return f'ACH-{self.achat_numero} ({self.achat_at})'


class Achat(models.Model):
    achat_code = models.ForeignKey(
        AchatCode,
        on_delete=models.RESTRICT,
        related_name='achats',
    )
    total_montant = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    total_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    nouveau_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    reste_a_payer = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    mode_paiement = models.CharField(max_length=10, choices=MODE_PAIEMENT_CHOICES, default='Espèces')
    statut = models.CharField(max_length=20, choices=STATUT_PAIEMENT_CHOICES, default='Payé')
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Achat'
        verbose_name_plural = 'Achats'

    def __str__(self):
        return f'{self.achat_code} — {self.total_montant} TND'


class AchatDetaille(models.Model):
    achat = models.ForeignKey(
        Achat,
        on_delete=models.CASCADE,
        related_name='details',
    )
    unite_pack = models.PositiveSmallIntegerField(default=1, help_text='Nombre d\'unités par pack')
    nombre_pack = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    prix_pack = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    matiere_premiere = models.ForeignKey(
        'production.MatierePremiere',
        on_delete=models.RESTRICT,
        null=True, blank=True,
        related_name='achat_details',
    )
    quantite = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    prix_unitaire = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    total_ligne = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    pack = models.BooleanField(default=False, help_text='Indique si l\'achat est en mode pack')

    class Meta:
        verbose_name = "Détail d'achat"
        verbose_name_plural = "Détails d'achats"

    def __str__(self):
        return f'{self.achat} — {self.matiere_premiere}'


class DepenseCode(models.Model):
    depense_at = models.DateField()
    depense_numero = models.SmallIntegerField()
    fournisseur = models.ForeignKey(
        Fournisseur,
        on_delete=models.RESTRICT,
        related_name='depenses_code',
    )

    class Meta:
        verbose_name = 'Code de dépense'
        verbose_name_plural = 'Codes de dépenses'
        ordering = ['-depense_at', 'depense_numero']
        constraints = [
            models.UniqueConstraint(
                fields=['depense_at', 'depense_numero'],
                name='unique_depense',
            ),
        ]

    def __str__(self):
        return f'DEP-{self.depense_numero} ({self.depense_at})'


class Depense(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.RESTRICT,
        related_name='depenses',
    )
    depense_code = models.ForeignKey(
        DepenseCode,
        on_delete=models.RESTRICT,
        related_name='depenses',
    )
    # decimal(11,8) : précision élevée pour les calculs de coûts unitaires
    total_montant = models.DecimalField(max_digits=11, decimal_places=8, default=0, validators=[MinValueValidator(0)])
    total_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    nouveau_acompte = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    reste_a_payer = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    mode_paiement = models.CharField(max_length=10, choices=MODE_PAIEMENT_CHOICES)
    statut = models.CharField(max_length=20, choices=STATUT_PAIEMENT_CHOICES, default='Non payé')
    observation = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = 'Dépense'
        verbose_name_plural = 'Dépenses'

    def __str__(self):
        return f'{self.depense_code} — {self.statut}'


class DepenseDetaille(models.Model):
    depense = models.ForeignKey(
        Depense,
        on_delete=models.CASCADE,
        related_name='details',
    )
    libelle = models.CharField(max_length=255, null=True, blank=True)
    quantite = models.DecimalField(max_digits=8, decimal_places=0, default=0, validators=[MinValueValidator(0)])
    valeur_unitaire = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    total_ligne = models.DecimalField(max_digits=11, decimal_places=3, default=0, validators=[MinValueValidator(0)])

    class Meta:
        verbose_name = 'Détail de dépense'
        verbose_name_plural = 'Détails de dépenses'

    def __str__(self):
        return f'{self.depense} — {self.libelle}'
