from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Gouvernorat, Delegation, Zone,
    Fournisseur, Client, ClientUser, Agenda,
    VenteCode, Vente, VenteDetaille,
    BonLivraisonCode, BonLivraison, BonLivraisonDetaille,
    AchatCode, Achat, AchatDetaille,
    DepenseCode, Depense, DepenseDetaille,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

_STATUT_STYLE = {
    'Payé':               ('#27ae60', '✔'),
    'Partiellement payé': ('#e67e22', '◑'),
    'Non payé':           ('#dc3545', '✗'),
}


def _badge_statut(statut):
    color, icon = _STATUT_STYLE.get(statut, ('#6c757d', '?'))
    return format_html(
        '<span style="color:{};font-weight:bold;">{} {}</span>',
        color, icon, statut or '—',
    )


# ─── Inlines géographie ───────────────────────────────────────────────────────

class DelegationInline(admin.TabularInline):
    model = Delegation
    extra = 0
    fields = ['nom_delegation']
    show_change_link = True
    verbose_name = 'Délégation'
    verbose_name_plural = 'Délégations'


class ZoneInline(admin.TabularInline):
    model = Zone
    extra = 0
    fields = ['nom']
    show_change_link = True
    verbose_name = 'Zone'
    verbose_name_plural = 'Zones'


# ─── Inlines client ───────────────────────────────────────────────────────────

class ClientUserInline(admin.TabularInline):
    model = ClientUser
    extra = 0
    autocomplete_fields = ['user']
    fields = ['user']
    verbose_name = 'Commercial assigné'
    verbose_name_plural = 'Commerciaux assignés'


class AgendaClientInline(admin.TabularInline):
    model = Agenda
    extra = 0
    fields = ['echeance_at', 'detaille_action_planifier', 'status', 'user']
    show_change_link = True
    verbose_name = 'Action planifiée'
    verbose_name_plural = 'Actions planifiées'


# ─── Inlines vente ────────────────────────────────────────────────────────────

class VenteInline(admin.TabularInline):
    model = Vente
    extra = 0
    fields = ['user', 'mode_paiement', 'statut', 'total_montant', 'reste_a_payer']
    readonly_fields = ['reste_a_payer']
    show_change_link = True


class VenteDetailleInline(admin.TabularInline):
    model = VenteDetaille
    extra = 0
    autocomplete_fields = ['bon_livraison_code']
    fields = ['bon_livraison_code']
    verbose_name = 'Bon de livraison lié'
    verbose_name_plural = 'Bons de livraison liés'


# ─── Inlines bon de livraison ─────────────────────────────────────────────────

class BonLivraisonInline(admin.TabularInline):
    model = BonLivraison
    extra = 0
    fields = ['user', 'mode_paiement', 'statut', 'total_montant', 'reste_a_payer']
    readonly_fields = ['reste_a_payer']
    show_change_link = True


class BonLivraisonDetailleInline(admin.TabularInline):
    model = BonLivraisonDetaille
    extra = 1
    autocomplete_fields = ['produit']
    fields = ['produit', 'quantite', 'prix_unitaire', 'total_ligne']
    verbose_name = 'Ligne produit'
    verbose_name_plural = 'Lignes produit'


# ─── Inlines achat ────────────────────────────────────────────────────────────

class AchatInline(admin.StackedInline):
    model = Achat
    extra = 0
    fields = [
        'mode_paiement', 'statut',
        'total_montant', 'total_acompte', 'nouveau_acompte', 'reste_a_payer',
        'observation',
    ]
    readonly_fields = ['reste_a_payer']
    show_change_link = True


class AchatDetailleInline(admin.TabularInline):
    model = AchatDetaille
    extra = 1
    autocomplete_fields = ['matiere_premiere']
    fields = ['matiere_premiere', 'pack', 'unite_pack', 'nombre_pack', 'prix_pack', 'quantite', 'prix_unitaire', 'total_ligne']
    verbose_name = "Ligne d'achat"
    verbose_name_plural = "Lignes d'achat"


# ─── Inlines dépense ─────────────────────────────────────────────────────────

class DepenseInline(admin.StackedInline):
    model = Depense
    extra = 0
    fields = [
        'user', 'mode_paiement', 'statut',
        'total_montant', 'total_acompte', 'nouveau_acompte', 'reste_a_payer',
        'observation',
    ]
    readonly_fields = ['reste_a_payer']
    show_change_link = True


class DepenseDetailleInline(admin.TabularInline):
    model = DepenseDetaille
    extra = 1
    fields = ['libelle', 'quantite', 'valeur_unitaire', 'total_ligne']
    verbose_name = 'Ligne de dépense'
    verbose_name_plural = 'Lignes de dépense'


# ─── Géographie ──────────────────────────────────────────────────────────────

@admin.register(Gouvernorat)
class GouvernoratAdmin(admin.ModelAdmin):
    list_display = ['nom', 'nb_delegations']
    search_fields = ['nom']
    ordering = ['nom']
    inlines = [DelegationInline]
    fieldsets = (
        (None, {'fields': ('nom',)}),
    )

    @admin.display(description='Nb délégations')
    def nb_delegations(self, obj):
        return obj.delegations.count()


@admin.register(Delegation)
class DelegationAdmin(admin.ModelAdmin):
    list_display = ['nom_delegation', 'gouvernorat', 'nb_zones']
    search_fields = ['nom_delegation', 'gouvernorat__nom']
    list_filter = ['gouvernorat']
    ordering = ['gouvernorat__nom', 'nom_delegation']
    autocomplete_fields = ['gouvernorat']
    inlines = [ZoneInline]
    fieldsets = (
        (None, {'fields': ('gouvernorat', 'nom_delegation')}),
    )

    @admin.display(description='Nb zones')
    def nb_zones(self, obj):
        return obj.zones.count()


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ['nom', 'delegation', 'gouvernorat_affiche', 'nb_clients']
    search_fields = ['nom', 'delegation__nom_delegation', 'delegation__gouvernorat__nom']
    list_filter = ['delegation__gouvernorat']
    ordering = ['delegation__nom_delegation', 'nom']
    autocomplete_fields = ['delegation']
    fieldsets = (
        (None, {'fields': ('delegation', 'nom')}),
    )

    @admin.display(description='Gouvernorat')
    def gouvernorat_affiche(self, obj):
        return obj.delegation.gouvernorat

    @admin.display(description='Nb clients')
    def nb_clients(self, obj):
        return obj.clients.count()


# ─── Fournisseur ─────────────────────────────────────────────────────────────

@admin.register(Fournisseur)
class FournisseurAdmin(admin.ModelAdmin):
    list_display = ['raison_sociale', 'nom_contact', 'type_fournisseur', 'telephone', 'email', 'created_at']
    search_fields = ['raison_sociale', 'nom_contact', 'telephone', 'email']
    list_filter = ['type_fournisseur']
    ordering = ['raison_sociale']
    list_per_page = 25
    fieldsets = (
        ('Identification', {
            'fields': ('raison_sociale', 'nom_contact', 'type_fournisseur', 'created_at'),
        }),
        ('Coordonnées', {
            'fields': ('adresse', 'telephone', 'email'),
        }),
        ('Informations financières', {
            'fields': ('tva', 'rib'),
            'classes': ('collapse',),
        }),
        ('Remarques', {
            'fields': ('observations',),
            'classes': ('collapse',),
        }),
    )


# ─── Client ──────────────────────────────────────────────────────────────────

@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ['nom_client', 'raison_sociale', 'zone', 'telephone', 'is_active', 'a_visiter_apres', 'created_at']
    search_fields = ['nom_client', 'raison_sociale', 'telephone']
    list_filter = ['is_active', 'zone__delegation__gouvernorat', 'zone__delegation']
    ordering = ['nom_client']
    readonly_fields = ['created_at']
    inlines = [ClientUserInline, AgendaClientInline]
    list_per_page = 25
    fieldsets = (
        ('Identification', {
            'fields': ('raison_sociale', 'nom_client', 'photo', 'is_active', 'created_at'),
        }),
        ('Localisation', {
            'fields': ('adresse', 'zone', 'google_mape'),
        }),
        ('Contact & Visite', {
            'fields': ('telephone', 'a_visiter_apres'),
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )


@admin.register(Agenda)
class AgendaAdmin(admin.ModelAdmin):
    list_display = ['client', 'echeance_at', 'user', 'status', 'detaille_action_planifier']
    search_fields = ['client__nom_client', 'client__raison_sociale', 'detaille_action_planifier']
    list_filter = ['status', 'echeance_at', 'user']
    ordering = ['echeance_at']
    autocomplete_fields = ['client', 'user']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('user', 'client', 'echeance_at', 'status'),
        }),
        ('Détail', {
            'fields': ('detaille_action_planifier',),
        }),
    )


# ─── Vente ───────────────────────────────────────────────────────────────────

@admin.register(VenteCode)
class VenteCodeAdmin(admin.ModelAdmin):
    list_display = ['vente_numero', 'vente_at', 'client', 'nb_bls', 'total_facture']
    search_fields = ['vente_numero', 'client__nom_client', 'client__raison_sociale']
    list_filter = ['vente_at']
    ordering = ['-vente_at', 'vente_numero']
    autocomplete_fields = ['client']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('client', 'vente_at', 'vente_numero'),
        }),
    )

    @admin.display(description='Nb BL')
    def nb_bls(self, obj):
        return obj.bons_livraison_code.count()

    @admin.display(description='Total facturé')
    def total_facture(self, obj):
        total = sum(v.total_montant for v in obj.ventes.all())
        return f'{total:.3f} TND' if total else '—'


@admin.register(Vente)
class VenteAdmin(admin.ModelAdmin):
    list_display = [
        'vente_code', 'user',
        'total_montant', 'total_acompte', 'reste_a_payer',
        'statut_affiche', 'mode_paiement',
    ]
    search_fields = ['vente_code__vente_numero', 'user__username', 'user__last_name']
    list_filter = ['statut', 'mode_paiement', 'vente_code__vente_at']
    ordering = ['-vente_code__vente_at']
    autocomplete_fields = ['vente_code', 'user']
    readonly_fields = ['reste_a_payer']
    inlines = [VenteDetailleInline]
    actions = ['marquer_payees']
    list_per_page = 25
    fieldsets = (
        ('Référence', {
            'fields': ('vente_code', 'user'),
        }),
        ('Règlement', {
            'fields': ('mode_paiement', 'statut', 'total_montant', 'total_acompte', 'nouveau_acompte', 'reste_a_payer'),
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Statut')
    def statut_affiche(self, obj):
        return _badge_statut(obj.statut)

    @admin.action(description='Marquer comme payées')
    def marquer_payees(self, request, queryset):
        nb = queryset.update(statut='Payé', reste_a_payer=0)
        self.message_user(request, f'{nb} vente(s) marquée(s) comme payée(s).')


@admin.register(BonLivraisonCode)
class BonLivraisonCodeAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'bon_livraison_at', 'client', 'vente_code', 'nb_livraisons']
    search_fields = ['client__nom_client', 'client__raison_sociale', 'vente_code__vente_numero']
    list_filter = ['bon_livraison_at']
    ordering = ['-bon_livraison_at', 'bon_livraison_numero']
    autocomplete_fields = ['client', 'vente_code']
    inlines = [BonLivraisonInline]
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('client', 'bon_livraison_at', 'bon_livraison_numero'),
        }),
        ('Liens', {
            'fields': ('vente_code', 'hist_stock_initial'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Nb livraisons')
    def nb_livraisons(self, obj):
        return obj.bons_livraison.count()


@admin.register(BonLivraison)
class BonLivraisonAdmin(admin.ModelAdmin):
    list_display = [
        'bon_livraison_code', 'user',
        'total_montant', 'reste_a_payer',
        'statut_affiche', 'mode_paiement',
    ]
    search_fields = [
        'bon_livraison_code__client__nom_client',
        'bon_livraison_code__client__raison_sociale',
        'user__username',
    ]
    list_filter = ['statut', 'mode_paiement', 'bon_livraison_code__bon_livraison_at']
    ordering = ['-bon_livraison_code__bon_livraison_at']
    autocomplete_fields = ['bon_livraison_code', 'user']
    readonly_fields = ['reste_a_payer']
    inlines = [BonLivraisonDetailleInline]
    actions = ['marquer_payes']
    list_per_page = 25
    fieldsets = (
        ('Référence', {
            'fields': ('bon_livraison_code', 'user'),
        }),
        ('Règlement', {
            'fields': ('mode_paiement', 'statut', 'total_montant', 'total_acompte', 'nouveau_acompte', 'reste_a_payer'),
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Statut')
    def statut_affiche(self, obj):
        return _badge_statut(obj.statut)

    @admin.action(description='Marquer comme payés')
    def marquer_payes(self, request, queryset):
        nb = queryset.update(statut='Payé', reste_a_payer=0)
        self.message_user(request, f'{nb} bon(s) de livraison marqué(s) comme payé(s).')


# ─── Achat ───────────────────────────────────────────────────────────────────

@admin.register(AchatCode)
class AchatCodeAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'achat_at', 'fournisseur', 'total_achat']
    search_fields = ['fournisseur__raison_sociale', 'fournisseur__nom_contact']
    list_filter = ['achat_at', 'fournisseur']
    ordering = ['-achat_at', 'achat_numero']
    autocomplete_fields = ['fournisseur']
    inlines = [AchatInline]
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('fournisseur', 'achat_at', 'achat_numero'),
        }),
    )

    @admin.display(description='Total achat')
    def total_achat(self, obj):
        total = sum(a.total_montant for a in obj.achats.all())
        return f'{total:.3f} TND' if total else '—'


@admin.register(Achat)
class AchatAdmin(admin.ModelAdmin):
    list_display = [
        'achat_code',
        'total_montant', 'reste_a_payer',
        'statut_affiche', 'mode_paiement',
    ]
    search_fields = ['achat_code__fournisseur__raison_sociale']
    list_filter = ['statut', 'mode_paiement', 'achat_code__achat_at']
    ordering = ['-achat_code__achat_at']
    autocomplete_fields = ['achat_code']
    readonly_fields = ['reste_a_payer']
    inlines = [AchatDetailleInline]
    actions = ['marquer_payes']
    list_per_page = 25
    fieldsets = (
        ('Référence', {
            'fields': ('achat_code',),
        }),
        ('Règlement', {
            'fields': ('mode_paiement', 'statut', 'total_montant', 'total_acompte', 'nouveau_acompte', 'reste_a_payer'),
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Statut')
    def statut_affiche(self, obj):
        return _badge_statut(obj.statut)

    @admin.action(description='Marquer comme payés')
    def marquer_payes(self, request, queryset):
        nb = queryset.update(statut='Payé', reste_a_payer=0)
        self.message_user(request, f'{nb} achat(s) marqué(s) comme payé(s).')


# ─── Dépense ─────────────────────────────────────────────────────────────────

@admin.register(DepenseCode)
class DepenseCodeAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'depense_at', 'fournisseur', 'total_depense']
    search_fields = ['fournisseur__raison_sociale', 'fournisseur__nom_contact']
    list_filter = ['depense_at', 'fournisseur']
    ordering = ['-depense_at', 'depense_numero']
    autocomplete_fields = ['fournisseur']
    inlines = [DepenseInline]
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('fournisseur', 'depense_at', 'depense_numero'),
        }),
    )

    @admin.display(description='Total dépense')
    def total_depense(self, obj):
        total = sum(d.total_montant for d in obj.depenses.all())
        return f'{float(total):.3f} TND' if total else '—'


@admin.register(Depense)
class DepenseAdmin(admin.ModelAdmin):
    list_display = [
        'depense_code', 'user',
        'total_montant', 'reste_a_payer',
        'statut_affiche', 'mode_paiement',
    ]
    search_fields = ['depense_code__fournisseur__raison_sociale', 'user__username', 'user__last_name']
    list_filter = ['statut', 'mode_paiement', 'depense_code__depense_at']
    ordering = ['-depense_code__depense_at']
    autocomplete_fields = ['depense_code', 'user']
    readonly_fields = ['reste_a_payer']
    inlines = [DepenseDetailleInline]
    actions = ['marquer_payees']
    list_per_page = 25
    fieldsets = (
        ('Référence', {
            'fields': ('depense_code', 'user'),
        }),
        ('Règlement', {
            'fields': ('mode_paiement', 'statut', 'total_montant', 'total_acompte', 'nouveau_acompte', 'reste_a_payer'),
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Statut')
    def statut_affiche(self, obj):
        return _badge_statut(obj.statut)

    @admin.action(description='Marquer comme payées')
    def marquer_payees(self, request, queryset):
        nb = queryset.update(statut='Payé', reste_a_payer=0)
        self.message_user(request, f'{nb} dépense(s) marquée(s) comme payée(s).')
