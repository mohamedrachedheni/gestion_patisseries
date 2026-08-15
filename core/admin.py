from django.contrib import admin

from .models import JourNonOuvre


@admin.register(JourNonOuvre)
class JourNonOuvreAdmin(admin.ModelAdmin):
    list_display = ['date', 'libelle', 'type_jour', 'concerne_livraison']
    search_fields = ['libelle']
    list_filter = ['type_jour', 'concerne_livraison']
    ordering = ['date']
    fieldsets = (
        (None, {'fields': ('date', 'libelle', 'type_jour', 'concerne_livraison')}),
    )
