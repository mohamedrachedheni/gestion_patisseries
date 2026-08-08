import os

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import View

from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin

from .models import Sauvegarde, TYPE_SAUVEGARDE_CHOICES
from .services import executer_restauration, executer_sauvegarde, lire_contenu_clair


class SauvegardeListView(GroupRequiredMixin, View):
    """Liste des sauvegardes de la base de données (app dédiée « backups »,
    réservée au groupe Administration). Section 1 « Filtres » : tri par Date
    (croissant/décroissant, décroissant par défaut), Chiffré et Réussie
    (cases à cocher, cochées par défaut — un champ caché « _soumis »
    accompagne chaque case pour distinguer un premier chargement, où le
    filtre par défaut s'applique, d'une soumission explicite où la case a
    été décochée). Section 2 : tableau paginé, colonnes Nom/Type/Chiffré/
    Taille/Date de création/Réussie/Actions (Télécharger, Restaurer,
    Supprimer — voir les vues dédiées ci-dessous)."""
    group_required = 'Administration'
    template_name = 'backups/sauvegarde/list.html'
    PAGINATE_BY = 15

    def get(self, request):
        ordre = request.GET.get('ordre', 'desc')
        if ordre not in ('asc', 'desc'):
            ordre = 'desc'

        chiffre_soumis = 'chiffre_soumis' in request.GET
        chiffre_filtre = ('chiffre' in request.GET) if chiffre_soumis else True

        reussie_soumis = 'reussie_soumis' in request.GET
        reussie_filtre = ('reussie' in request.GET) if reussie_soumis else True

        qs = Sauvegarde.objects.filter(
            chiffre=chiffre_filtre, reussie=reussie_filtre,
        ).order_by('created_at' if ordre == 'asc' else '-created_at')

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page'))

        return render(request, self.template_name, {
            'page_obj':        page_obj,
            'ordre':           ordre,
            'chiffre_filtre':  chiffre_filtre,
            'reussie_filtre':  reussie_filtre,
        })


class SauvegardeCreateView(GroupRequiredMixin, View):
    """Page « Ajout de Sauvegarde de base de données ». Déclenche une VRAIE
    sauvegarde (mysqldump, voir backups/services.py) au moment de
    l'enregistrement — Nom/Type/Chiffré/Observation sont saisis par
    l'utilisateur ; Taille/Date de création/Réussie sont calculés côté
    serveur à partir du résultat réel de mysqldump."""
    group_required = 'Administration'
    template_name = 'backups/sauvegarde/create.html'

    def _context(self, **overrides):
        context = {
            'type_sauvegarde_choices': TYPE_SAUVEGARDE_CHOICES,
            'nom_value': f'sauvegarde_{timezone.now():%Y%m%d_%H%M%S}',
            'observation_value': '',
        }
        context.update(overrides)
        return context

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        nom = request.POST.get('nom', '').strip()
        type_sauvegarde = request.POST.get('type_sauvegarde', '').strip()
        chiffre = request.POST.get('chiffre') == 'on'
        observation = request.POST.get('observation', '').strip()

        if not nom:
            messages.error(request, 'Le nom de la sauvegarde est obligatoire.')
            return render(request, self.template_name, self._context(
                nom_value=nom, observation_value=observation,
            ))
        if type_sauvegarde not in dict(TYPE_SAUVEGARDE_CHOICES):
            messages.error(request, 'Le type de sauvegarde est invalide.')
            return render(request, self.template_name, self._context(
                nom_value=nom, observation_value=observation,
            ))

        sauvegarde = Sauvegarde.objects.create(
            nom=nom, type_sauvegarde=type_sauvegarde, chiffre=chiffre,
            observation=observation or None,
            cree_par=request.user.get_full_name() or request.user.username, reussie=False,
        )
        resultat = executer_sauvegarde(sauvegarde)

        if resultat.ok:
            log_audit(
                AuditAction.DB_BACKUP, f'Sauvegarde base de données « {nom} » ({type_sauvegarde})',
                table='Sauvegarde', record_id=sauvegarde.pk,
            )
            messages.success(request, f'Sauvegarde « {nom} » créée avec succès.')
        else:
            messages.error(request, f"Échec de la sauvegarde « {nom} » : {resultat.erreur}")

        return redirect('backups:sauvegarde-list')


class SauvegardeDownloadView(GroupRequiredMixin, View):
    """Bouton « Télécharger » — sert le contenu SQL en clair (déchiffré à la
    volée si la sauvegarde est chiffrée ; jamais de fichier déchiffré écrit
    sur disque)."""
    group_required = 'Administration'

    def get(self, request, pk):
        sauvegarde = get_object_or_404(Sauvegarde, pk=pk)
        if not sauvegarde.reussie or not sauvegarde.chemin_fichier:
            messages.error(request, 'Cette sauvegarde ne peut pas être téléchargée.')
            return redirect('backups:sauvegarde-list')

        try:
            contenu = lire_contenu_clair(sauvegarde)
        except (OSError, ValueError) as exc:
            messages.error(request, f'Échec du téléchargement : {exc}')
            return redirect('backups:sauvegarde-list')

        log_audit(
            AuditAction.DB_BACKUP, f'Téléchargement sauvegarde « {sauvegarde.nom} »',
            table='Sauvegarde', record_id=sauvegarde.pk,
        )
        response = HttpResponse(contenu, content_type='application/sql')
        response['Content-Disposition'] = f'attachment; filename="{sauvegarde.nom}.sql"'
        return response


class SauvegardeRestoreView(GroupRequiredMixin, View):
    """Bouton « Restaurer » — remplace tout le contenu de la base de données
    par celui de `sauvegarde` (opération destructive sur l'état courant).
    Réservé aux sauvegardes de type 'Complète' : une sauvegarde 'Structure
    seule' (--no-data) ne contient AUCUNE donnée et 'Données seules'
    (--no-create-info) AUCUNE structure — restaurer l'une ou l'autre
    remplacerait toutes les tables par un contenu partiel et effacerait le
    reste de la base (constaté en test réel : une restauration 'Structure
    seule' a vidé toutes les tables métier). Avant toute restauration
    (Complète), une sauvegarde de sécurité de l'état ACTUEL est créée
    automatiquement ; si elle échoue, la restauration est annulée — jamais de
    restauration sans filet de sécurité fraîchement pris."""
    group_required = 'Administration'

    def post(self, request, pk):
        sauvegarde = Sauvegarde.objects.filter(pk=pk).first()
        if sauvegarde is None:
            messages.error(request, "L'enregistrement est déjà supprimé.")
            return redirect('backups:sauvegarde-list')
        if not sauvegarde.reussie or not sauvegarde.chemin_fichier:
            messages.error(request, 'Cette sauvegarde ne peut pas être restaurée.')
            return redirect('backups:sauvegarde-list')
        if sauvegarde.type_sauvegarde != 'Complète':
            # Une sauvegarde « Structure seule » ne contient aucune donnée et
            # « Données seules » aucune structure (--no-create-info) : la
            # restaurer remplacerait TOUTES les tables par ce contenu partiel
            # et effacerait le reste de la base. Seule une sauvegarde
            # « Complète » peut être restaurée sans perte.
            messages.error(
                request,
                f'Seule une sauvegarde de type « Complète » peut être restaurée '
                f'(cette sauvegarde est de type « {sauvegarde.type_sauvegarde} »).',
            )
            return redirect('backups:sauvegarde-list')

        securite = Sauvegarde.objects.create(
            nom=f'auto-avant-restauration-{timezone.now():%Y%m%d_%H%M%S}',
            type_sauvegarde='Complète', chiffre=True, reussie=False,
            cree_par=request.user.get_full_name() or request.user.username,
            observation=f'Sauvegarde de sécurité automatique avant restauration de « {sauvegarde.nom} ».',
        )
        resultat_securite = executer_sauvegarde(securite)
        if not resultat_securite.ok:
            messages.error(
                request,
                'La restauration a été annulée : impossible de créer la sauvegarde de sécurité '
                f'préalable ({resultat_securite.erreur}).',
            )
            return redirect('backups:sauvegarde-list')

        resultat = executer_restauration(sauvegarde)
        if resultat.ok:
            log_audit(
                AuditAction.DB_RESTORE, f'Restauration base de données depuis « {sauvegarde.nom} »',
                table='Sauvegarde', record_id=sauvegarde.pk,
            )
            messages.success(request, f'Base de données restaurée avec succès depuis « {sauvegarde.nom} ».')
        else:
            messages.error(request, f'Échec de la restauration : {resultat.erreur}')

        return redirect('backups:sauvegarde-list')


class SauvegardeDeleteView(GroupRequiredMixin, View):
    """Bouton « Supprimer » — idempotent (comme les autres *DeleteView de
    l'application) : supprime aussi le fichier de sauvegarde sur disque, le
    cas échéant."""
    group_required = 'Administration'

    def post(self, request, pk):
        sauvegarde = Sauvegarde.objects.filter(pk=pk).first()
        if sauvegarde is None:
            messages.info(request, 'Cet enregistrement a déjà été supprimé.')
            return redirect('backups:sauvegarde-list')

        if sauvegarde.chemin_fichier:
            chemin_absolu = os.path.join(settings.DB_BACKUPS_DIR, sauvegarde.chemin_fichier)
            if os.path.isfile(chemin_absolu):
                os.remove(chemin_absolu)

        nom = sauvegarde.nom
        sauvegarde.delete()
        log_audit(AuditAction.DELETE, f'Suppression sauvegarde « {nom} »', table='Sauvegarde', record_id=pk)
        messages.success(request, f'Sauvegarde « {nom} » supprimée avec succès.')
        return redirect('backups:sauvegarde-list')
