from django.contrib import admin
from django.utils.html import format_html

from .models import Employe, HistoriqueSoldeCompte, Transaction, Transfere


# ─── Inlines ─────────────────────────────────────────────────────────────────

class HistoriqueSoldeCompteInline(admin.TabularInline):
    model = HistoriqueSoldeCompte
    extra = 0
    fields = ['solde_at', 'calcul_solde', 'correction_solde', 'observation', 'admin']
    readonly_fields = ['calcul_solde']
    show_change_link = True
    verbose_name = 'Historique de solde'
    verbose_name_plural = 'Historiques de solde'


# ─── Employé ─────────────────────────────────────────────────────────────────

@admin.register(Employe)
class EmployeAdmin(admin.ModelAdmin):
    list_display = [
        '__str__', 'matricule', 'service',
        'type_contrat', 'telephone', 'embauche_at', 'salaire',
    ]
    search_fields = [
        'user__first_name', 'user__last_name',
        'user__username', 'matricule', 'service',
    ]
    list_filter = ['service', 'type_contrat']
    ordering = ['user__last_name', 'user__first_name']
    autocomplete_fields = ['user']
    list_per_page = 25
    fieldsets = (
        ('Compte utilisateur', {
            'fields': ('user',),
            'description': 'Le compte Django associé à cet employé.',
        }),
        ('Identité', {
            'fields': ('matricule', 'photo', 'naissance_at', 'telephone', 'adresse'),
        }),
        ('Contrat', {
            'fields': ('service', 'type_contrat', 'embauche_at', 'salaire'),
        }),
        ('Remarques', {
            'fields': ('observation',),
            'classes': ('collapse',),
        }),
    )


# ─── Historique solde compte ──────────────────────────────────────────────────

@admin.register(HistoriqueSoldeCompte)
class HistoriqueSoldeCompteAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'solde_at',
        'calcul_solde', 'correction_solde', 'solde_final',
        'admin', 'observation',
    ]
    search_fields = [
        'user__username', 'user__first_name', 'user__last_name',
        'admin__username',
    ]
    list_filter = ['solde_at', 'user']
    ordering = ['-solde_at']
    autocomplete_fields = ['user', 'admin']
    readonly_fields = ['calcul_solde']
    list_per_page = 25
    fieldsets = (
        ('Période', {
            'fields': ('user', 'solde_at'),
        }),
        ('Soldes', {
            'fields': ('calcul_solde', 'correction_solde'),
            'description': 'Le solde calculé est généré automatiquement. Seule la correction est modifiable.',
        }),
        ('Validation', {
            'fields': ('admin', 'observation'),
        }),
    )

    @admin.display(description='Solde final')
    def solde_final(self, obj):
        total = obj.calcul_solde + obj.correction_solde
        color = '#27ae60' if total >= 0 else '#dc3545'
        return format_html(
            '<span style="color:{};font-weight:bold;">{:.3f} TND</span>',
            color, total,
        )

    def has_add_permission(self, request):
        return request.user.is_staff

    def has_change_permission(self, request, obj=None):
        # Seul un superutilisateur peut modifier un historique de solde validé
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# ─── Transaction caisse ───────────────────────────────────────────────────────

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = [
        'created_at', 'user', 'operation',
        'table_id', 'montant_affiche',
    ]
    search_fields = ['operation', 'table_id', 'user__username', 'user__last_name']
    list_filter = ['created_at', 'user']
    ordering = ['-created_at']
    # Toutes les colonnes sont en lecture seule : journal non modifiable
    readonly_fields = ['created_at', 'user', 'operation', 'table_id', 'montant']
    list_per_page = 50
    fieldsets = (
        ('Opération', {
            'fields': ('user', 'created_at', 'operation', 'table_id'),
        }),
        ('Montant', {
            'fields': ('montant',),
        }),
    )

    @admin.display(description='Montant', ordering='montant')
    def montant_affiche(self, obj):
        color = '#27ae60' if obj.montant >= 0 else '#dc3545'
        signe = '+' if obj.montant >= 0 else ''
        return format_html(
            '<span style="color:{};font-weight:bold;">{}{:.3f} TND</span>',
            color, signe, obj.montant,
        )

    def has_add_permission(self, request):
        # Les transactions sont créées uniquement via le code métier
        return False

    def has_change_permission(self, request, obj=None):
        # Journal d'audit : aucune modification autorisée
        return False

    def has_delete_permission(self, request, obj=None):
        # Seul un superutilisateur peut supprimer (cas exceptionnel)
        return request.user.is_superuser


# ─── Transfert entre comptes ──────────────────────────────────────────────────

@admin.register(Transfere)
class TransfereAdmin(admin.ModelAdmin):
    list_display = [
        'transfere_at', 'compte_source',
        'compte_destination', 'libelle', 'montant_affiche',
    ]
    search_fields = [
        'compte_source__username', 'compte_source__last_name',
        'compte_destination__username', 'compte_destination__last_name',
        'libelle',
    ]
    list_filter = ['transfere_at', 'compte_source', 'compte_destination']
    ordering = ['-transfere_at']
    autocomplete_fields = ['compte_source', 'compte_destination']
    list_per_page = 25
    fieldsets = (
        (None, {
            'fields': ('transfere_at', 'libelle'),
        }),
        ('Comptes', {
            'fields': ('compte_source', 'compte_destination', 'montant'),
            'description': 'Indiquer le compte débité (source) et le compte crédité (destination).',
        }),
    )

    @admin.display(description='Montant', ordering='montant')
    def montant_affiche(self, obj):
        return format_html(
            '<span style="font-weight:bold;">{:.3f} TND</span>', obj.montant
        )

    def has_add_permission(self, request):
        return request.user.is_staff

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser
