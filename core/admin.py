from django.contrib import admin

from .models import JourNonOuvre, VerrouillageConnexion


@admin.register(JourNonOuvre)
class JourNonOuvreAdmin(admin.ModelAdmin):
    list_display = ['date', 'libelle', 'type_jour', 'concerne_livraison']
    search_fields = ['libelle']
    list_filter = ['type_jour', 'concerne_livraison']
    ordering = ['date']
    fieldsets = (
        (None, {'fields': ('date', 'libelle', 'type_jour', 'concerne_livraison')}),
    )


@admin.register(VerrouillageConnexion)
class VerrouillageConnexionAdmin(admin.ModelAdmin):
    """Permet à un administrateur de déverrouiller un compte manuellement
    (suppression de la ligne) sans attendre la fin du verrouillage."""
    list_display = ['identifiant', 'echecs', 'verrouille_jusqu_a']
    search_fields = ['identifiant']
    ordering = ['-verrouille_jusqu_a']
