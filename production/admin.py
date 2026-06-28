from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Produit, MatierePremiereFamille, MatierePremiere,
    Recette, RecetteDetaille, CommandeInterne,
    HistoriqueStockInitial, HistoriqueStockInitialDetaille,
    BonRestitution, BonSortie,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _badge_stock(quantite, seuil):
    if quantite is None:
        return format_html('<span style="color:#6c757d;">—</span>')
    if quantite <= 0:
        color, label = '#dc3545', 'Rupture'
    elif quantite <= (seuil or 0):
        color, label = '#e67e22', 'Faible'
    else:
        color, label = '#27ae60', 'OK'
    return format_html(
        '<span style="color:{};font-weight:bold;">{}</span>', color, label
    )


# ─── Inlines ─────────────────────────────────────────────────────────────────

class RecetteDetailleInline(admin.TabularInline):
    model = RecetteDetaille
    extra = 1
    autocomplete_fields = ['matiere_premiere']
    fields = ['matiere_premiere', 'quantite']
    verbose_name = 'Ingrédient'
    verbose_name_plural = 'Ingrédients'


class MatierePremiereInline(admin.TabularInline):
    model = MatierePremiere
    extra = 0
    fields = ['nom', 'unite', 'quantite', 'stock_minimum']
    show_change_link = True
    verbose_name = 'Matière première'
    verbose_name_plural = 'Matières premières'


class HistoriqueStockInitialDetailleInline(admin.TabularInline):
    model = HistoriqueStockInitialDetaille
    extra = 0
    autocomplete_fields = ['produit']
    fields = ['produit', 'calcul_stock', 'reel_stock', 'observation']
    verbose_name = 'Ligne de stock'
    verbose_name_plural = 'Lignes de stock'


# ─── Produit ─────────────────────────────────────────────────────────────────

@admin.register(Produit)
class ProduitAdmin(admin.ModelAdmin):
    list_display = [
        'nom', 'taille', 'prix_unitaire', 'poids',
        'stock', 'quantite_minimale', 'statut_stock',
        'is_produit_semi_fini',
    ]
    search_fields = ['nom']
    list_filter = ['taille', 'is_produit_semi_fini']
    ordering = ['nom']
    readonly_fields = ['stock']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('nom', 'taille', 'poids', 'prix_unitaire', 'is_produit_semi_fini'),
        }),
        ('Stock', {
            'fields': ('stock', 'quantite_minimale'),
            'description': 'Le stock courant est mis à jour automatiquement.',
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Statut stock', ordering='stock')
    def statut_stock(self, obj):
        return _badge_stock(obj.stock, obj.quantite_minimale)


# ─── Matière première ─────────────────────────────────────────────────────────

@admin.register(MatierePremiereFamille)
class MatierePremiereFamilleAdmin(admin.ModelAdmin):
    list_display = ['famille', 'nb_matieres', 'observation']
    search_fields = ['famille']
    ordering = ['famille']
    inlines = [MatierePremiereInline]
    fieldsets = (
        (None, {
            'fields': ('famille', 'observation'),
        }),
    )

    @admin.display(description='Nb matières')
    def nb_matieres(self, obj):
        return obj.matieres_premieres.count()


@admin.register(MatierePremiere)
class MatierePremiereAdmin(admin.ModelAdmin):
    list_display = [
        'nom', 'matiere_premiere_famille', 'unite',
        'quantite', 'stock_minimum', 'statut_stock', 'produit',
    ]
    search_fields = ['nom']
    list_filter = ['unite', 'matiere_premiere_famille']
    ordering = ['nom']
    autocomplete_fields = ['matiere_premiere_famille', 'produit']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('nom', 'matiere_premiere_famille', 'unite'),
        }),
        ('Stock', {
            'fields': ('quantite', 'stock_minimum'),
        }),
        ('Produit semi-fini', {
            'fields': ('produit',),
            'description': 'À renseigner uniquement si cette matière est un produit semi-fini.',
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Statut', ordering='quantite')
    def statut_stock(self, obj):
        return _badge_stock(obj.quantite, obj.stock_minimum)


# ─── Recette ─────────────────────────────────────────────────────────────────

@admin.register(Recette)
class RecetteAdmin(admin.ModelAdmin):
    list_display = ['produit', 'quantite', 'nb_ingredients']
    search_fields = ['produit__nom']
    ordering = ['produit__nom']
    autocomplete_fields = ['produit']
    inlines = [RecetteDetailleInline]
    fieldsets = (
        (None, {
            'fields': ('produit', 'quantite', 'observation'),
        }),
    )

    @admin.display(description='Nb ingrédients')
    def nb_ingredients(self, obj):
        return obj.details.count()


# ─── Commande interne ─────────────────────────────────────────────────────────

@admin.register(CommandeInterne)
class CommandeInterneAdmin(admin.ModelAdmin):
    list_display = [
        'produit', 'livraison_at',
        'quantite_commander', 'quantite_produite',
        'statut_commande', 'is_stock_maj',
    ]
    search_fields = ['produit__nom']
    list_filter = ['is_completed', 'is_stock_maj', 'livraison_at']
    ordering = ['-livraison_at']
    autocomplete_fields = ['produit']
    actions = ['marquer_completees', 'marquer_stock_maj']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('produit', 'livraison_at'),
        }),
        ('Quantités', {
            'fields': ('quantite_commander', 'quantite_produite'),
        }),
        ('Suivi', {
            'fields': ('is_completed', 'is_stock_maj'),
        }),
    )

    @admin.display(description='Statut')
    def statut_commande(self, obj):
        if obj.is_completed:
            return format_html('<span style="color:#27ae60;font-weight:bold;">✔ Complétée</span>')
        return format_html('<span style="color:#e67e22;font-weight:bold;">⏳ En cours</span>')

    @admin.action(description='Marquer comme complétées')
    def marquer_completees(self, request, queryset):
        nb = queryset.update(is_completed=True)
        self.message_user(request, f'{nb} commande(s) marquée(s) comme complétée(s).')

    @admin.action(description='Marquer stock mis à jour')
    def marquer_stock_maj(self, request, queryset):
        nb = queryset.update(is_stock_maj=True)
        self.message_user(request, f'Stock mis à jour pour {nb} commande(s).')


# ─── Historique stock initial ─────────────────────────────────────────────────

@admin.register(HistoriqueStockInitial)
class HistoriqueStockInitialAdmin(admin.ModelAdmin):
    list_display = ['stock_initial_at', 'user', 'nb_produits']
    search_fields = ['user__username', 'user__first_name', 'user__last_name']
    list_filter = ['stock_initial_at']
    ordering = ['-stock_initial_at']
    readonly_fields = ['stock_initial_at', 'user']
    inlines = [HistoriqueStockInitialDetailleInline]
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('user', 'stock_initial_at'),
        }),
    )

    @admin.display(description='Nb produits')
    def nb_produits(self, obj):
        return obj.details.count()


# ─── Bon de restitution ───────────────────────────────────────────────────────

@admin.register(BonRestitution)
class BonRestitutionAdmin(admin.ModelAdmin):
    list_display = ['id', 'restitution_at', 'user', 'produit', 'quantite', 'hist_stock_initial']
    search_fields = ['produit__nom', 'user__username', 'user__last_name']
    list_filter = ['restitution_at']
    ordering = ['-restitution_at']
    autocomplete_fields = ['produit']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('user', 'restitution_at', 'produit', 'quantite'),
        }),
        ('Lien stock', {
            'fields': ('hist_stock_initial',),
            'classes': ('collapse',),
        }),
    )


# ─── Bon de sortie ────────────────────────────────────────────────────────────

@admin.register(BonSortie)
class BonSortieAdmin(admin.ModelAdmin):
    list_display = ['id', 'sortie_at', 'user', 'produit', 'quantite', 'hist_stock_initial']
    search_fields = ['produit__nom', 'user__username', 'user__last_name']
    list_filter = ['sortie_at']
    ordering = ['-sortie_at']
    autocomplete_fields = ['produit']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('user', 'sortie_at', 'produit', 'quantite'),
        }),
        ('Lien stock', {
            'fields': ('hist_stock_initial',),
            'classes': ('collapse',),
        }),
    )
