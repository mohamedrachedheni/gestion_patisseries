import base64
import datetime
import json
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max, Q, RestrictedError, Sum
from django.forms.models import model_to_dict
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, View
from xhtml2pdf import pisa

from commercial.models import (
    Achat, AchatDetaille, BonLivraison, BonLivraisonDetaille, Delegation, Depense, DepenseDetaille,
    Gouvernorat, Vente, Zone,
)
from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin
from production.models import CommandeInterne, MatierePremiere, Produit, Recette, RecetteDetaille
from production.services import (
    calculer_besoins_matieres_premieres_periode,
    calculer_besoins_matieres_premieres_pour_semi_finis_periode,
    quantite_commandee_semi_finis_periode,
    recettes_produit_unite_map,
    resoudre_ingredients_recette,
)

from .forms import (
    DelegationForm,
    EmployeForm,
    GouvernoratForm,
    TransfereForm,
    UserCreateForm,
    UserUpdateForm,
    ZoneForm,
)
from .models import Employe, HistoriqueSoldeCompte, LibelleTransaction, Transaction as TransactionModel, Transfere

User = get_user_model()


def _form_errors_text(form):
    return ' '.join(e for errors in form.errors.values() for e in errors) or 'Données invalides.'


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = 'Administration'
    template_name = 'administration/home.html'


# ─── Gouvernorats ───────────────────────────────────────────────────────────

class GouvernoratListView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/gouvernorat/list.html'
    PAGINATE_BY = 5

    def get(self, request):
        qs = Gouvernorat.objects.order_by('nom')
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':      page_obj,
            'is_paginated':  page_obj.has_other_pages(),
            'gouvernorats':  page_obj.object_list,
            'add_form':      GouvernoratForm(),
        })


class GouvernoratCreateView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request):
        form = GouvernoratForm(request.POST)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('administration:gouvernorat-list')

        gouvernorat = form.save()
        log_audit(
            AuditAction.CREATE, f'Création gouvernorat « {gouvernorat.nom} »',
            table='Gouvernorat', record_id=gouvernorat.pk, new_value=model_to_dict(gouvernorat),
        )
        messages.success(request, f'Gouvernorat « {gouvernorat.nom} » ajouté avec succès.')
        return redirect('administration:gouvernorat-list')


class GouvernoratUpdateView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request, pk):
        instance = get_object_or_404(Gouvernorat, pk=pk)
        avant = model_to_dict(instance)
        form = GouvernoratForm(request.POST, instance=instance)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('administration:gouvernorat-list')

        gouvernorat = form.save()
        log_audit(
            AuditAction.UPDATE, f'Modification gouvernorat « {gouvernorat.nom} »',
            table='Gouvernorat', record_id=gouvernorat.pk, old_value=avant, new_value=model_to_dict(gouvernorat),
        )
        messages.success(request, f'Gouvernorat « {gouvernorat.nom} » modifié avec succès.')
        return redirect('administration:gouvernorat-list')


class GouvernoratDeleteView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request, pk):
        instance = Gouvernorat.objects.filter(pk=pk).first()
        if instance is None:
            messages.info(request, 'Ce gouvernorat a déjà été supprimé.')
            return redirect('administration:gouvernorat-list')
        nom = instance.nom
        avant = model_to_dict(instance)
        try:
            instance.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : des délégations sont encore "
                "rattachées à ce gouvernorat.",
            )
        else:
            log_audit(
                AuditAction.DELETE, f'Suppression gouvernorat « {nom} »',
                table='Gouvernorat', record_id=pk, old_value=avant,
            )
            messages.success(request, f'Gouvernorat « {nom} » supprimé avec succès.')
        return redirect('administration:gouvernorat-list')


# ─── Délégations ────────────────────────────────────────────────────────────

class DelegationListView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/delegation/list.html'
    PAGINATE_BY = 20

    def get(self, request):
        gouvernorat_id = request.GET.get('gouvernorat_id', '').strip()

        qs = Delegation.objects.select_related('gouvernorat').order_by('gouvernorat__nom', 'nom_delegation')
        if gouvernorat_id:
            qs = qs.filter(gouvernorat_id=gouvernorat_id)

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'delegations':       page_obj.object_list,
            'gouvernorat_id':    gouvernorat_id,
            'gouvernorats_list': Gouvernorat.objects.order_by('nom'),
            'add_form':          DelegationForm(),
        })


class DelegationCreateView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request):
        form = DelegationForm(request.POST)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('administration:delegation-list')

        delegation = form.save()
        log_audit(
            AuditAction.CREATE, f'Création délégation « {delegation.nom_delegation} »',
            table='Delegation', record_id=delegation.pk, new_value=model_to_dict(delegation),
        )
        messages.success(request, f'Délégation « {delegation.nom_delegation} » ajoutée avec succès.')
        return redirect('administration:delegation-list')


class DelegationUpdateView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request, pk):
        instance = get_object_or_404(Delegation, pk=pk)
        avant = model_to_dict(instance)
        form = DelegationForm(request.POST, instance=instance)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('administration:delegation-list')

        delegation = form.save()
        log_audit(
            AuditAction.UPDATE, f'Modification délégation « {delegation.nom_delegation} »',
            table='Delegation', record_id=delegation.pk, old_value=avant, new_value=model_to_dict(delegation),
        )
        messages.success(request, f'Délégation « {delegation.nom_delegation} » modifiée avec succès.')
        return redirect('administration:delegation-list')


class DelegationDeleteView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request, pk):
        instance = Delegation.objects.filter(pk=pk).first()
        if instance is None:
            messages.info(request, 'Cette délégation a déjà été supprimée.')
            return redirect('administration:delegation-list')
        nom = instance.nom_delegation
        avant = model_to_dict(instance)
        try:
            instance.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : des zones sont encore "
                "rattachées à cette délégation.",
            )
        else:
            log_audit(
                AuditAction.DELETE, f'Suppression délégation « {nom} »',
                table='Delegation', record_id=pk, old_value=avant,
            )
            messages.success(request, f'Délégation « {nom} » supprimée avec succès.')
        return redirect('administration:delegation-list')


# ─── Zones ──────────────────────────────────────────────────────────────────

class ZoneListView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/zone/list.html'
    PAGINATE_BY = 20

    def get(self, request):
        gouvernorat_id = request.GET.get('gouvernorat', '').strip()
        delegation_id = request.GET.get('delegation', '').strip()

        qs = Zone.objects.select_related('delegation__gouvernorat').order_by(
            'delegation__gouvernorat__nom', 'delegation__nom_delegation', 'nom',
        )
        if gouvernorat_id:
            qs = qs.filter(delegation__gouvernorat_id=gouvernorat_id)
        if delegation_id:
            qs = qs.filter(delegation_id=delegation_id)

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        delegations_list = Delegation.objects.select_related('gouvernorat').order_by(
            'gouvernorat__nom', 'nom_delegation',
        )

        return render(request, self.template_name, {
            'page_obj':          page_obj,
            'is_paginated':      page_obj.has_other_pages(),
            'zones':             page_obj.object_list,
            'gouvernorat_id':    gouvernorat_id,
            'delegation_id':     delegation_id,
            'gouvernorats_list': Gouvernorat.objects.order_by('nom'),
            'delegations_list':  delegations_list,
            'delegations_json':  json.dumps([
                {'id': d.pk, 'nom': d.nom_delegation, 'gouvernorat_id': d.gouvernorat_id}
                for d in delegations_list
            ]),
        })


class ZoneCreateView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request):
        gouvernorat_id = request.POST.get('gouvernorat', '').strip()
        delegation_id = request.POST.get('delegation', '').strip()
        nom = request.POST.get('nom', '').strip()

        if not gouvernorat_id:
            messages.error(request, 'Le champ gouvernorat est obligatoire.')
            return redirect('administration:zone-list')
        if not delegation_id:
            messages.error(request, 'Le champ délégation est obligatoire.')
            return redirect('administration:zone-list')
        if not nom:
            messages.error(request, 'Le champ zone est obligatoire.')
            return redirect('administration:zone-list')

        delegation = Delegation.objects.filter(pk=delegation_id).first()
        if delegation is None:
            messages.error(request, 'Délégation introuvable.')
            return redirect('administration:zone-list')
        if str(delegation.gouvernorat_id) != gouvernorat_id:
            messages.error(
                request,
                "La délégation sélectionnée n'appartient pas au gouvernorat choisi.",
            )
            return redirect('administration:zone-list')

        if Zone.objects.filter(nom=nom).exists():
            messages.error(request, f"Une zone nommée « {nom} » existe déjà.")
            return redirect('administration:zone-list')

        zone = Zone.objects.create(delegation=delegation, nom=nom)
        log_audit(
            AuditAction.CREATE, f'Création zone « {zone.nom} »',
            table='Zone', record_id=zone.pk, new_value=model_to_dict(zone),
        )
        messages.success(request, f'Zone « {zone.nom} » ajoutée avec succès.')
        return redirect('administration:zone-list')


class ZoneUpdateView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request, pk):
        instance = get_object_or_404(Zone, pk=pk)
        avant = model_to_dict(instance)
        form = ZoneForm(request.POST, instance=instance)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('administration:zone-list')

        zone = form.save()
        log_audit(
            AuditAction.UPDATE, f'Modification zone « {zone.nom} »',
            table='Zone', record_id=zone.pk, old_value=avant, new_value=model_to_dict(zone),
        )
        messages.success(request, f'Zone « {zone.nom} » modifiée avec succès.')
        return redirect('administration:zone-list')


class ZoneDeleteView(GroupRequiredMixin, View):
    group_required = 'Administration'

    def post(self, request, pk):
        instance = Zone.objects.filter(pk=pk).first()
        if instance is None:
            messages.info(request, 'Cette zone a déjà été supprimée.')
            return redirect('administration:zone-list')
        nom = instance.nom
        avant = model_to_dict(instance)
        try:
            instance.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : des clients sont encore "
                "rattachés à cette zone.",
            )
        else:
            log_audit(
                AuditAction.DELETE, f'Suppression zone « {nom} »',
                table='Zone', record_id=pk, old_value=avant,
            )
            messages.success(request, f'Zone « {nom} » supprimée avec succès.')
        return redirect('administration:zone-list')


# ─── Employés ───────────────────────────────────────────────────────────────

class EmployeListView(GroupRequiredMixin, ListView):
    group_required = 'Administration'
    template_name = 'administration/employe/list.html'
    context_object_name = 'employes'
    paginate_by = 20

    def get_queryset(self):
        qs = Employe.objects.select_related('user').prefetch_related('user__groups')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(user__first_name__icontains=q) |
                Q(user__last_name__icontains=q)  |
                Q(matricule__icontains=q)         |
                Q(service__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['q'] = self.request.GET.get('q', '')
        return ctx


class EmployeDetailView(GroupRequiredMixin, DetailView):
    group_required = 'Administration'
    template_name = 'administration/employe/detail.html'
    context_object_name = 'employe'
    queryset = Employe.objects.select_related('user')


class EmployeCreateView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/employe/form.html'

    def _forms(self, data=None, files=None):
        return UserCreateForm(data), EmployeForm(data, files, hide_matricule_service=True)

    def get(self, request):
        user_form, employe_form = self._forms()
        return render(request, self.template_name, {
            'user_form': user_form,
            'employe_form': employe_form,
            'title': 'Nouvel employé',
        })

    def post(self, request):
        user_form, employe_form = self._forms(request.POST, request.FILES)
        if user_form.is_valid() and employe_form.is_valid():
            with transaction.atomic():
                user = user_form.save()
                employe = employe_form.save(commit=False)
                employe.user = user
                employe.save()
            log_audit(
                AuditAction.CREATE, f'Création employé {user.get_full_name()}',
                table='Employe', record_id=employe.pk, new_value=model_to_dict(employe),
            )
            messages.success(request, f'Employé {user.get_full_name()} créé avec succès.')
            return redirect('administration:employe-detail', pk=employe.pk)
        return render(request, self.template_name, {
            'user_form': user_form,
            'employe_form': employe_form,
            'title': 'Nouvel employé',
        })


class EmployeUpdateView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/employe/form.html'

    def _get(self, pk):
        return get_object_or_404(Employe.objects.select_related('user'), pk=pk)

    def get(self, request, pk):
        employe = self._get(pk)
        return render(request, self.template_name, {
            'user_form': UserUpdateForm(instance=employe.user),
            'employe_form': EmployeForm(instance=employe, hide_matricule_service=True),
            'employe': employe,
            'title': f'Modifier — {employe.user.get_full_name()}',
            'is_update': True,
        })

    def post(self, request, pk):
        employe = self._get(pk)
        avant = model_to_dict(employe)
        user_form = UserUpdateForm(request.POST, instance=employe.user)
        employe_form = EmployeForm(request.POST, request.FILES, instance=employe, hide_matricule_service=True)
        if user_form.is_valid() and employe_form.is_valid():
            with transaction.atomic():
                user = user_form.save()
                employe_form.save()
            if user.pk == request.user.pk:
                update_session_auth_hash(request, user)
            log_audit(
                AuditAction.UPDATE, f'Modification employé {employe.user.get_full_name()}',
                table='Employe', record_id=employe.pk, old_value=avant, new_value=model_to_dict(employe),
            )
            messages.success(request, 'Employé mis à jour avec succès.')
            return redirect('administration:employe-detail', pk=employe.pk)
        return render(request, self.template_name, {
            'user_form': user_form,
            'employe_form': employe_form,
            'employe': employe,
            'title': f'Modifier — {employe.user.get_full_name()}',
            'is_update': True,
        })


class EmployeDeleteView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/employe/confirm_delete.html'

    def _get(self, pk):
        return Employe.objects.select_related('user').filter(pk=pk).first()

    def get(self, request, pk):
        employe = self._get(pk)
        if employe is None:
            messages.info(request, 'Cet employé a déjà été supprimé.')
            return redirect('administration:employe-list')
        return render(request, self.template_name, {'employe': employe})

    def post(self, request, pk):
        employe = self._get(pk)
        if employe is None:
            messages.info(request, 'Cet employé a déjà été supprimé.')
            return redirect('administration:employe-list')
        user = employe.user
        nom = user.get_full_name()
        avant = model_to_dict(employe)
        try:
            with transaction.atomic():
                employe.delete()
                user.delete()
        except RestrictedError:
            # employe.delete() met employe.pk à None en mémoire même si la
            # transaction est ensuite annulée : on utilise le pk d'origine.
            messages.error(
                request,
                f"Impossible de supprimer {nom} : son compte utilisateur est encore "
                "lié à d'autres enregistrements (transactions, ventes, transferts, "
                "historiques…). Ces liens doivent être traités avant toute suppression.",
            )
            return redirect('administration:employe-detail', pk=pk)

        log_audit(
            AuditAction.DELETE, f'Suppression employé {nom}',
            table='Employe', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Employé {nom} et son compte utilisateur ont été supprimés.')
        return redirect('administration:employe-list')


# ─── Transactions ────────────────────────────────────────────────────────────

class TransactionListView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/transaction/list.html'
    PAGINATE_BY = 25

    def get(self, request):
        today = date.today()

        # ── Lecture des filtres GET ───────────────────────────────
        date_debut_str    = request.GET.get('date_debut', '').strip()
        date_fin_str      = request.GET.get('date_fin',   '').strip()
        user_id           = request.GET.get('user_id',    '').strip()
        libelle_id        = request.GET.get('libelle_id', '').strip()
        montant_non_zero  = request.GET.get('montant_non_zero', '').strip()

        # Valeurs par défaut : 7 derniers jours
        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        # ── Queryset filtré ───────────────────────────────────────
        # USE_TZ = False : datetimes naïfs, comparaison directe sans CONVERT_TZ
        debut_dt = datetime.datetime.combine(date_debut, datetime.time.min)
        fin_dt   = datetime.datetime.combine(date_fin,   datetime.time.max)

        qs = TransactionModel.objects.select_related('user', 'libelle').filter(
            created_at__gte=debut_dt,
            created_at__lte=fin_dt,
        )
        if user_id:
            qs = qs.filter(user_id=user_id)
        if libelle_id:
            qs = qs.filter(libelle_id=libelle_id)
        if montant_non_zero:
            qs = qs.exclude(montant=0)

        qs = qs.order_by('-created_at', '-pk')

        # ── Agrégats résumé ───────────────────────────────────────
        totaux = qs.aggregate(
            credit=Sum('montant', filter=Q(montant__gte=0)),
            debit=Sum('montant',  filter=Q(montant__lt=0)),
        )
        totaux['net'] = (totaux['credit'] or 0) + (totaux['debit'] or 0)

        # ── Données pour les listes déroulantes ───────────────────
        users_list = (
            User.objects
            .filter(groups__name__in=['Administration', 'Commercial', 'Production'])
            .distinct()
            .order_by('last_name', 'first_name')
        )
        libelles_list = LibelleTransaction.objects.order_by('libelle')

        # ── Pagination ────────────────────────────────────────────
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':       page_obj,
            'is_paginated':   page_obj.has_other_pages(),
            'date_debut':     date_debut_str,
            'date_fin':       date_fin_str,
            'user_id':        user_id,
            'libelle_id':     libelle_id,
            'montant_non_zero': montant_non_zero,
            'users_list':     users_list,
            'libelles_list':  libelles_list,
            'totaux':         totaux,
            'total_count':    paginator.count,
        })


_BON_LIVRAISON_TABLE_ID_RE          = re.compile(r'^BonLivraison_id_(\d+)$')
_HISTORIQUE_SOLDE_COMPTE_TABLE_ID_RE = re.compile(r'^historiquesoldecompte_id_(\d+)$')
_ACHAT_TABLE_ID_RE                  = re.compile(r'^Achat_id_(\d+)$')
_DEPENSE_TABLE_ID_RE                = re.compile(r'^Depense_id_(\d+)$')
_TRANSFERE_VERSE_TABLE_ID_RE        = re.compile(r'^transfere_verse_id_(\d+)$')
_TRANSFERE_RECU_TABLE_ID_RE         = re.compile(r'^transfere_recu_id_(\d+)$')
_VENTE_TABLE_ID_RE                  = re.compile(r'^Vente_id_(\d+)$')

_POPUP_TITLES = {
    'bon_livraison':          'Détail bon de livraison',
    'historique_solde_compte': 'Liste des transactions caisse liées au solde compte caisse sélectionné',
    'achat':                  'Détail achat',
    'depense':                'Détail dépense',
    'transfere_verse':        'Transfère versé',
    'transfere_recu':         'Transfère reçu',
    'vente':                  "Historique des modification d'une vente",
}


class TransactionDetailPopupView(GroupRequiredMixin, View):
    """Contenu (fragment HTML, chargé en AJAX dans la pop-up « Détail ») de la
    page Transactions — le gabarit affiché dépend du format de
    transaction.table_id :

    - « BonLivraison_id_<id> » : Section 1 (BonLivraisonCode — sur une seule
      ligne), Section 1-1 (BonLivraison liés au même bon_livraison_code_id,
      paginés un enregistrement à la fois, page par défaut = celui référencé
      par la transaction), Section 1-1-1 (BonLivraisonDetaille du
      BonLivraison affiché, sans paginateur).
    - « historiquesoldecompte_id_<id> » : pop-up « Les quantités de
      produits... » — reprend administration/soldes-caisse/<pk>/transactions/
      (Section 1 Données solde caisse, Section 2 champs calculés Transactions/
      Crédits/Débits/Solde net, Section 3 même tableau sans le bouton Détail),
      sans paginateur.
    - « Achat_id_<id> » : Section 1 (AchatCode), Section 1-1 (l'Achat
      sélectionné — Employé = transaction.user, Achat n'a pas de champ user
      propre —, sans paginateur), Section 1-1-1 (AchatDetaille, sans
      paginateur).
    - « Depense_id_<id> » : même structure que Achat_id_ (Section 1
      DepenseCode, Section 1-1 la Depense — Employé = depense.user —,
      Section 1-1-1 DepenseDetaille).
    - « transfere_verse_id_<id> » / « transfere_recu_id_<id> » : Transfere
      (Employé émetteur/receveur, Date, Montant, Libellé), sur deux lignes.
    - « Vente_id_<id> » : même conception que le bouton Visualiser de
      commercial/ventes/ (VenteHistoriquePopupView) — Section 1 (VenteCode),
      Section 2 « Historique de la vente » paginée un enregistrement Vente à
      la fois (page par défaut = celui référencé par la transaction),
      Sections 3/4 (BL inclus / produits regroupés) recalculées pour
      l'enregistrement Vente courant du paginateur.
    - tout autre format : « Détail non disponible pour cette transaction »."""
    group_required = 'Administration'
    template_name = 'administration/transaction/detail_popup.html'

    def get(self, request, pk):
        transaction_obj = get_object_or_404(TransactionModel, pk=pk)
        table_id = transaction_obj.table_id or ''

        popup_type = None
        response = None

        match = _BON_LIVRAISON_TABLE_ID_RE.match(table_id)
        if match:
            popup_type = 'bon_livraison'
            response = self._render_bon_livraison(request, transaction_obj, int(match.group(1)))

        match = _HISTORIQUE_SOLDE_COMPTE_TABLE_ID_RE.match(table_id) if response is None else None
        if match:
            popup_type = 'historique_solde_compte'
            response = self._render_historique_solde_compte(request, transaction_obj, int(match.group(1)))

        match = _ACHAT_TABLE_ID_RE.match(table_id) if response is None else None
        if match:
            popup_type = 'achat'
            response = self._render_achat(request, transaction_obj, int(match.group(1)))

        match = _DEPENSE_TABLE_ID_RE.match(table_id) if response is None else None
        if match:
            popup_type = 'depense'
            response = self._render_depense(request, transaction_obj, int(match.group(1)))

        match = _TRANSFERE_VERSE_TABLE_ID_RE.match(table_id) if response is None else None
        if match:
            popup_type = 'transfere_verse'
            response = self._render_transfere(request, transaction_obj, int(match.group(1)), popup_type)

        match = _TRANSFERE_RECU_TABLE_ID_RE.match(table_id) if response is None else None
        if match:
            popup_type = 'transfere_recu'
            response = self._render_transfere(request, transaction_obj, int(match.group(1)), popup_type)

        match = _VENTE_TABLE_ID_RE.match(table_id) if response is None else None
        if match:
            popup_type = 'vente'
            response = self._render_vente(request, transaction_obj, int(match.group(1)))

        if response is None:
            response = render(request, self.template_name, {
                'type':        None,
                'transaction': transaction_obj,
            })

        response['X-Popup-Title'] = _POPUP_TITLES.get(popup_type, 'Détail')
        return response

    def _render_bon_livraison(self, request, transaction_obj, bon_livraison_id):
        bon_livraison_ref = get_object_or_404(BonLivraison, pk=bon_livraison_id)
        code = bon_livraison_ref.bon_livraison_code

        historique_qs = BonLivraison.objects.filter(
            bon_livraison_code_id=code.pk,
        ).select_related('user').order_by('id')

        historique_ids = list(historique_qs.values_list('id', flat=True))
        try:
            page_par_defaut = historique_ids.index(bon_livraison_id) + 1
        except ValueError:
            page_par_defaut = 1

        paginator = Paginator(historique_qs, 1)
        page_obj = paginator.get_page(request.GET.get('page') or page_par_defaut)
        bon_livraison = page_obj.object_list[0] if page_obj.object_list else None
        details = (
            bon_livraison.details.select_related('produit').order_by('pk')
            if bon_livraison else BonLivraisonDetaille.objects.none()
        )

        return render(request, self.template_name, {
            'type':               'bon_livraison',
            'transaction':        transaction_obj,
            'bon_livraison_code': code,
            'page_obj':           page_obj,
            'bon_livraison':      bon_livraison,
            'details':            details,
            # Page.previous_page_number()/next_page_number() lèvent EmptyPage
            # (non silencieuse) quand la page n'existe pas — jamais les
            # appeler côté template sans garde ; on précalcule ici des
            # valeurs toujours sûres (repliées sur la page courante).
            'prev_page': page_obj.previous_page_number() if page_obj.has_previous() else page_obj.number,
            'next_page': page_obj.next_page_number() if page_obj.has_next() else page_obj.number,
        })

    def _render_historique_solde_compte(self, request, transaction_obj, hist_id):
        hist = get_object_or_404(HistoriqueSoldeCompte.objects.select_related('user'), pk=hist_id)

        transactions_qs = (
            TransactionModel.objects
            .select_related('user', 'libelle')
            .filter(hist_solde_compte_id=hist.pk)
            .order_by('-created_at', '-pk')
        )
        totaux = transactions_qs.aggregate(
            credit=Sum('montant', filter=Q(montant__gte=0)),
            debit=Sum('montant', filter=Q(montant__lt=0)),
        )
        totaux['net'] = (totaux['credit'] or 0) + (totaux['debit'] or 0)

        return render(request, self.template_name, {
            'type':          'historique_solde_compte',
            'transaction':   transaction_obj,
            'hist':          hist,
            'transactions':  transactions_qs,
            'total_count':   transactions_qs.count(),
            'totaux':        totaux,
        })

    def _render_achat(self, request, transaction_obj, achat_id):
        achat = get_object_or_404(Achat.objects.select_related('achat_code__fournisseur'), pk=achat_id)
        details = achat.details.select_related('matiere_premiere').order_by('pk')

        return render(request, self.template_name, {
            'type':        'achat',
            'transaction': transaction_obj,
            # Achat n'a pas de champ user propre : l'Employé affiché est
            # celui de la transaction elle-même.
            'employe':     transaction_obj.user,
            'obj':         achat,
            'code':        achat.achat_code,
            'details':     details,
        })

    def _render_depense(self, request, transaction_obj, depense_id):
        depense = get_object_or_404(
            Depense.objects.select_related('depense_code__fournisseur', 'user'), pk=depense_id,
        )
        details = depense.details.order_by('pk')

        return render(request, self.template_name, {
            'type':        'depense',
            'transaction': transaction_obj,
            'employe':     depense.user,
            'obj':         depense,
            'code':        depense.depense_code,
            'details':     details,
        })

    def _render_transfere(self, request, transaction_obj, transfere_id, type_):
        transfere = get_object_or_404(
            Transfere.objects.select_related('compte_source', 'compte_destination'), pk=transfere_id,
        )
        return render(request, self.template_name, {
            'type':        type_,
            'transaction': transaction_obj,
            'transfere':   transfere,
        })

    def _render_vente(self, request, transaction_obj, vente_id):
        vente_ref = get_object_or_404(Vente.objects.select_related('vente_code__client'), pk=vente_id)
        vente_code = vente_ref.vente_code

        historique_qs = Vente.objects.filter(
            vente_code_id=vente_code.pk,
        ).select_related('user').order_by('id')
        historique_ids = list(historique_qs.values_list('id', flat=True))
        try:
            page_par_defaut = historique_ids.index(vente_id) + 1
        except ValueError:
            page_par_defaut = 1

        paginator = Paginator(historique_qs, 1)
        page_obj = paginator.get_page(request.GET.get('page') or page_par_defaut)
        vente = page_obj.object_list[0] if page_obj.object_list else None

        lignes_bl = []
        groupes_produits = []
        if vente is not None:
            bl_code_ids = list(vente.details.values_list('bon_livraison_code_id', flat=True))
            dernier_bl_id_par_code = (
                BonLivraison.objects
                .filter(bon_livraison_code_id__in=bl_code_ids)
                .values('bon_livraison_code_id')
                .annotate(dernier_id=Max('id'))
                .values_list('dernier_id', flat=True)
            )
            derniers_bl = (
                BonLivraison.objects
                .filter(pk__in=dernier_bl_id_par_code)
                .select_related('bon_livraison_code')
                .order_by('bon_livraison_code__bon_livraison_at', 'bon_livraison_code__bon_livraison_numero')
            )
            lignes_bl = [
                {
                    'date':          bl.bon_livraison_code.bon_livraison_at,
                    'numero':        bl.bon_livraison_code.bon_livraison_numero,
                    'montant_total': bl.total_montant,
                    'montant_paye':  bl.total_acompte,
                    'reste':         bl.reste_a_payer,
                }
                for bl in derniers_bl
            ]
            lignes_produits = (
                BonLivraisonDetaille.objects
                .filter(bon_livraison_id__in=dernier_bl_id_par_code, produit__isnull=False)
                .values('produit__nom', 'prix_unitaire')
                .annotate(quantite=Sum('quantite'))
                .order_by('produit__nom')
            )
            groupes_produits = [
                {
                    'produit_nom':   l['produit__nom'],
                    'prix_unitaire': l['prix_unitaire'],
                    'quantite':      l['quantite'],
                    'total_ligne':   l['prix_unitaire'] * l['quantite'],
                }
                for l in lignes_produits
            ]

        return render(request, self.template_name, {
            'type':             'vente',
            'transaction':      transaction_obj,
            'vente_code':       vente_code,
            'page_obj':         page_obj,
            'vente':            vente,
            'lignes_bl':        lignes_bl,
            'groupes_produits': groupes_produits,
            'prev_page': page_obj.previous_page_number() if page_obj.has_previous() else page_obj.number,
            'next_page': page_obj.next_page_number() if page_obj.has_next() else page_obj.number,
        })


# ─── Transferts ──────────────────────────────────────────────────────────────

def _libelles_transfere_distincts():
    return (
        Transfere.objects
        .exclude(libelle__isnull=True).exclude(libelle='')
        .values_list('libelle', flat=True)
        .distinct()
        .order_by('libelle')
    )


class TransfereCreateView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/transfere/form.html'

    def _form(self, data=None):
        initial = {
            'transfere_at': date.today(),
            'compte_source': self.request.user,
            'libelle': 'remise caisse',
            'montant': 0,
        }
        return TransfereForm(data, initial=initial)

    def _context(self, form):
        return {
            'form': form,
            'title': 'Nouveau transfert',
            'libelles_transfere': _libelles_transfere_distincts(),
        }

    def get(self, request):
        return render(request, self.template_name, self._context(self._form()))

    def post(self, request):
        form = self._form(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, self._context(form))

        transfere = form.save(commit=False)
        try:
            with transaction.atomic():
                transfere.save()
                transfere.creer_transactions()
        except Exception:
            messages.error(
                request,
                "Échec de l'enregistrement du transfert : aucune donnée n'a été modifiée.",
            )
            return render(request, self.template_name, self._context(form))

        log_audit(
            AuditAction.CREATE,
            f'Création transfert {transfere.compte_source} → {transfere.compte_destination} '
            f'({transfere.montant} TND)',
            table='Transfere', record_id=transfere.pk, new_value=model_to_dict(transfere),
        )
        messages.success(
            request,
            f'Transfert de {transfere.montant} TND enregistré avec succès.',
        )
        return redirect('administration:transfere-create')


class TransfereUpdateView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/transfere/form.html'

    def _get(self, pk):
        return get_object_or_404(
            Transfere.objects.select_related('compte_source', 'compte_destination'), pk=pk,
        )

    def _context(self, form, transfere):
        return {
            'form': form,
            'title': 'Modifier le transfert',
            'transfere': transfere,
            'is_update': True,
            'libelles_transfere': _libelles_transfere_distincts(),
        }

    def get(self, request, pk):
        transfere = self._get(pk)
        if transfere.a_transactions_soldees():
            messages.error(
                request,
                "La modification de ce transfert est impossible, car il a déjà "
                "été utilisé pour le calcul du solde d'un compte.",
            )
            return redirect('administration:transfere-list')
        return render(request, self.template_name, self._context(TransfereForm(instance=transfere), transfere))

    def post(self, request, pk):
        transfere_avant = self._get(pk)
        if transfere_avant.a_transactions_soldees():
            messages.error(
                request,
                "La modification de ce transfert est impossible, car il a déjà "
                "été utilisé pour le calcul du solde d'un compte.",
            )
            return redirect('administration:transfere-list')
        valeurs_avant = model_to_dict(transfere_avant)
        form = TransfereForm(request.POST, instance=transfere_avant)
        if not form.is_valid():
            return render(request, self.template_name, self._context(form, transfere_avant))

        transfere = form.save(commit=False)
        try:
            with transaction.atomic():
                transfere.save()
                transfere.modifier_transactions_liees()
        except TransactionModel.DoesNotExist:
            messages.error(
                request,
                "Une incohérence a été détectée entre la table Transfere et les "
                "enregistrements associés de la table Transaction. Veuillez "
                "supprimer l'enregistrement concerné dans la table Transfere, "
                "puis en créer un nouveau.",
            )
            return render(request, self.template_name, self._context(form, transfere_avant))
        except Exception:
            messages.error(
                request,
                "Échec de la modification du transfert : aucune donnée n'a été modifiée.",
            )
            return render(request, self.template_name, self._context(form, transfere_avant))

        log_audit(
            AuditAction.UPDATE, f'Modification transfert #{transfere.pk}',
            table='Transfere', record_id=transfere.pk,
            old_value=valeurs_avant, new_value=model_to_dict(transfere),
        )
        messages.success(request, 'Transfert modifié avec succès.')
        return redirect('administration:transfere-list')


class TransfereDeleteView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/transfere/confirm_delete.html'

    def _get(self, pk):
        return Transfere.objects.select_related('compte_source', 'compte_destination').filter(pk=pk).first()

    def get(self, request, pk):
        transfere = self._get(pk)
        if transfere is None:
            messages.info(request, 'Ce transfert a déjà été supprimé.')
            return redirect('administration:transfere-list')
        if transfere.a_transactions_soldees():
            messages.error(
                request,
                "La suppression de ce transfert est impossible, car il a déjà "
                "été utilisé pour le calcul du solde d'un compte.",
            )
            return redirect('administration:transfere-list')
        return render(request, self.template_name, {'transfere': transfere})

    def post(self, request, pk):
        transfere = self._get(pk)
        if transfere is None:
            messages.info(request, 'Ce transfert a déjà été supprimé.')
            return redirect('administration:transfere-list')
        if transfere.a_transactions_soldees():
            messages.error(
                request,
                "La suppression de ce transfert est impossible, car il a déjà "
                "été utilisé pour le calcul du solde d'un compte.",
            )
            return redirect('administration:transfere-list')
        valeurs_avant = model_to_dict(transfere)
        with transaction.atomic():
            transfere.supprimer_transactions_liees()
            transfere.delete()
        log_audit(
            AuditAction.DELETE, f'Suppression transfert #{pk}',
            table='Transfere', record_id=pk, old_value=valeurs_avant,
        )
        messages.success(request, 'Transfert supprimé avec succès.')
        return redirect('administration:transfere-list')


class TransfereListView(GroupRequiredMixin, View):
    group_required = 'Administration'
    template_name = 'administration/transfere/list.html'
    PAGINATE_BY = 25

    def get(self, request):
        today = date.today()

        # ── Lecture des filtres GET ───────────────────────────────
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin',   '').strip()
        libelle        = request.GET.get('libelle',    '').strip()
        operateur_id   = request.GET.get('operateur_id', '').strip()

        # Valeurs par défaut : 7 derniers jours
        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        # ── Queryset filtré ────────────────────────────────────────
        qs = Transfere.objects.select_related('compte_source', 'compte_destination').filter(
            transfere_at__gte=date_debut,
            transfere_at__lte=date_fin,
        )
        if libelle:
            qs = qs.filter(libelle=libelle)
        if operateur_id:
            qs = qs.filter(compte_source_id=operateur_id)

        qs = qs.order_by('-transfere_at', '-pk')

        # ── Données pour les listes déroulantes ───────────────────
        operateurs_list = (
            User.objects
            .filter(groups__name__in=['Administration', 'Commercial', 'Production'])
            .distinct()
            .order_by('last_name', 'first_name')
        )
        libelles_list = _libelles_transfere_distincts()
        # Comptes proposés dans les selects Compte source/Compte destination de chaque ligne
        users_list = (
            User.objects
            .filter(groups__name__in=['Administration', 'Commercial', 'Production'])
            .distinct()
            .order_by('last_name', 'first_name')
        )

        # ── Pagination ────────────────────────────────────────────
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':        page_obj,
            'is_paginated':    page_obj.has_other_pages(),
            'date_debut':      date_debut_str,
            'date_fin':        date_fin_str,
            'libelle':         libelle,
            'operateur_id':    operateur_id,
            'operateurs_list': operateurs_list,
            'libelles_list':   libelles_list,
            'users_list':      users_list,
            'total_count':     paginator.count,
        })


# ─── Rupture de stock ───────────────────────────────────────────────────────

def _construire_ruptures_matieres(besoins, derniers_achat_ids, date_limite_60j):
    """Construit, pour chaque matière première de `besoins`, sa ligne avec
    Manque = Besoin + Stock minimum − Stock disponible, son dernier
    prix/fournisseur d'achat et le meilleur prix des 60 derniers jours (un
    même AchatCode peut avoir plusieurs Achat — historique, voir
    AchatUpdateView — on ne considère que le dernier de chacun, via
    `derniers_achat_ids`). Toutes les matières premières de `besoins` sont
    incluses, y compris celles sans manque réel (Manque ≤ 0). Retourne la
    liste triée par nom de matière première."""
    ruptures = []
    if not besoins:
        return ruptures

    matieres = {mp.pk: mp for mp in MatierePremiere.objects.filter(pk__in=besoins.keys())}
    for mp_id, besoin in besoins.items():
        mp = matieres.get(mp_id)
        if mp is None:
            continue

        stock_disponible = mp.quantite or Decimal('0')
        stock_minimum = mp.stock_minimum or Decimal('0')
        manque = besoin + stock_minimum - stock_disponible

        dernier = (
            AchatDetaille.objects
            .filter(matiere_premiere_id=mp_id, achat_id__in=derniers_achat_ids)
            .select_related('achat__achat_code__fournisseur')
            .order_by('-achat__achat_code__achat_at', '-achat_id')
            .first()
        )
        meilleur_60j = (
            AchatDetaille.objects
            .filter(
                matiere_premiere_id=mp_id, achat_id__in=derniers_achat_ids,
                achat__achat_code__achat_at__gte=date_limite_60j,
            )
            .select_related('achat__achat_code__fournisseur')
            .order_by('prix_unitaire', '-achat__achat_code__achat_at')
            .first()
        )

        ruptures.append({
            'matiere_premiere':         mp,
            'besoin':                   besoin,
            'stock_disponible':         stock_disponible,
            'stock_minimum':            stock_minimum,
            'manque':                   manque,
            'manque_affiche':           -manque,
            'dernier_prix':             dernier.prix_unitaire if dernier else None,
            'dernier_fournisseur':      dernier.achat.achat_code.fournisseur if dernier else None,
            'dernier_date':             dernier.achat.achat_code.achat_at if dernier else None,
            'meilleur_prix_60j':        meilleur_60j.prix_unitaire if meilleur_60j else None,
            'meilleur_fournisseur_60j': meilleur_60j.achat.achat_code.fournisseur if meilleur_60j else None,
            'meilleur_date_60j':        meilleur_60j.achat.achat_code.achat_at if meilleur_60j else None,
        })

    ruptures.sort(key=lambda r: r['matiere_premiere'].nom)
    return ruptures


class RuptureStockView(GroupRequiredMixin, View):
    """« Contrôle de rupture de stock par période pour les commandes internes
    non achevées » — Partie 1 : ne prend en compte que les commandes internes
    non achevées de PRODUITS FINIS de la période (les commandes de produits
    semi-finis eux-mêmes sont exclues), et uniquement les matières premières
    « de base » de leurs recettes — les ingrédients qui sont eux-mêmes des
    produits semi-finis sont exclus du tableau (voir
    production.services.calculer_besoins_matieres_premieres_periode). Calcule,
    pour chaque matière première, Manque = Besoin + Stock minimum − Stock
    disponible (Stock disponible = MatierePremiere.quantite). Toutes les
    matières premières concernées sont listées, y compris celles sans manque
    réel (Manque ≤ 0 — pas de filtrage), avec leur dernier prix/fournisseur
    d'achat et le meilleur prix des 60 derniers jours.

    Partie 2 : produits semi-finis — combine le besoin brut en semi-finis
    (ingrédients des recettes d'autres produits commandés,
    calculer_besoins_matieres_premieres_periode) et les commandes internes
    non achevées portant directement sur un produit semi-fini. Pour chacun,
    Manque = Besoin + Stock minimum − Stock disponible − Quantité commandée.
    Tous les produits concernés sont listés, y compris ceux dont le Manque
    est ≤ 0.

    Partie 3 : mêmes colonnes que la partie 1 (même absence de filtrage), mais
    pour la PRODUCTION des produits semi-finis eux-mêmes — quantité à
    produire = besoin en tant qu'ingrédient (partie 2) + quantité commandée
    directement, développée RÉCURSIVEMENT via la Recette de chaque semi-fini
    (voir production.services.calculer_besoins_matieres_premieres_pour_semi_finis_periode)
    jusqu'à n'obtenir que des matières premières de base.

    Partie 4 : synthèse — mêmes colonnes que les parties 1 et 3, mais
    regroupe (somme) leurs besoins par matière première puis ne conserve que
    celles présentant un manque réel (Manque > 0 — filtrage, contrairement
    aux parties 1 et 3).

    Lecture seule."""
    group_required = 'Administration'
    template_name = 'administration/rupture_stock/list.html'

    def get(self, request):
        today = date.today()

        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin',   '').strip()

        # Par défaut : les sept prochains jours, aujourd'hui inclus.
        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today + timedelta(days=6)
            date_fin_str = date_fin.isoformat()

        if date_debut > date_fin:
            messages.error(request, 'La date de début doit être antérieure ou égale à la date de fin.')
            date_debut = today
            date_fin = today + timedelta(days=6)
            date_debut_str = date_debut.isoformat()
            date_fin_str = date_fin.isoformat()

        besoins, besoins_semi_fini, erreurs = calculer_besoins_matieres_premieres_periode(date_debut, date_fin)
        for message in erreurs:
            messages.warning(request, message)

        # Achats : un même AchatCode peut avoir plusieurs Achat (historique,
        # voir AchatUpdateView) — on ne considère que le dernier de chacun.
        derniers_achat_ids = (
            Achat.objects.values('achat_code_id').annotate(dernier_id=Max('id')).values_list('dernier_id', flat=True)
        )
        date_limite_60j = today - timedelta(days=60)

        ruptures = _construire_ruptures_matieres(besoins, derniers_achat_ids, date_limite_60j)

        # ── Partie 2 : produits semi-finis en rupture ──────────────────────
        # Quantité déjà commandée (non achevée) pour chaque produit semi-fini
        # de la période — réduit d'autant le manque à produire/acheter.
        quantite_commandee_par_produit = quantite_commandee_semi_finis_periode(date_debut, date_fin)

        matieres_semi_finies = {
            mp.produit_id: mp
            for mp in MatierePremiere.objects.filter(pk__in=besoins_semi_fini.keys()).select_related('produit')
        }
        # Compléter avec les semi-finis commandés directement mais sans besoin
        # en tant qu'ingrédient (sinon absents de matieres_semi_finies).
        produits_ids_manquants = set(quantite_commandee_par_produit) - set(matieres_semi_finies)
        if produits_ids_manquants:
            for mp in MatierePremiere.objects.filter(produit_id__in=produits_ids_manquants).select_related('produit'):
                matieres_semi_finies[mp.produit_id] = mp

        ruptures_semi_finis = []
        for produit_id, mp in matieres_semi_finies.items():
            besoin = besoins_semi_fini.get(mp.pk, Decimal('0'))
            stock_disponible = mp.quantite or Decimal('0')
            stock_minimum = mp.stock_minimum or Decimal('0')
            quantite_commandee = quantite_commandee_par_produit.get(produit_id, Decimal('0'))
            manque = besoin + stock_minimum - stock_disponible - quantite_commandee
            ruptures_semi_finis.append({
                'produit':             mp.produit,
                'unite':               mp.unite,
                'besoin':              besoin,
                'stock_disponible':    stock_disponible,
                'stock_minimum':       stock_minimum,
                'quantite_commandee':  quantite_commandee,
                'manque':              manque,
                'manque_affiche':      -manque,
            })

        ruptures_semi_finis.sort(key=lambda r: r['produit'].nom)

        # ── Partie 3 : matières premières pour la production des semi-finis ─
        besoins_pour_semi_finis, erreurs_semi_finis = calculer_besoins_matieres_premieres_pour_semi_finis_periode(
            date_debut, date_fin,
        )
        for message in erreurs_semi_finis:
            messages.warning(request, message)
        ruptures_pour_semi_finis = _construire_ruptures_matieres(
            besoins_pour_semi_finis, derniers_achat_ids, date_limite_60j,
        )

        # ── Partie 4 : synthèse — matières premières pour toute la production ─
        # Regroupe (somme) les besoins des parties 1 et 3, puis ne conserve que
        # les matières premières présentant un manque réel (Manque > 0).
        besoins_totaux = {}
        for mp_id, quantite in besoins.items():
            besoins_totaux[mp_id] = besoins_totaux.get(mp_id, Decimal('0')) + quantite
        for mp_id, quantite in besoins_pour_semi_finis.items():
            besoins_totaux[mp_id] = besoins_totaux.get(mp_id, Decimal('0')) + quantite

        ruptures_totaux = [
            r for r in _construire_ruptures_matieres(besoins_totaux, derniers_achat_ids, date_limite_60j)
            if r['manque'] > 0
        ]

        # ── Nouvelle partie : commandes internes de la période non encore produites ─
        commandes_internes_periode = (
            CommandeInterne.objects
            .filter(
                livraison_at__gte=date_debut, livraison_at__lte=date_fin,
                is_stock_maj=False, is_completed=False,
            )
            .select_related('recette__produit')
            .order_by('livraison_at')
        )
        produits_avec_recette = Produit.objects.filter(recettes__isnull=False).order_by('nom')
        rupture_stock_url = (
            f"{reverse('administration:rupture-stock-list')}"
            f"?date_debut={date_debut_str}&date_fin={date_fin_str}"
        )

        return render(request, self.template_name, {
            'date_debut':                  date_debut_str,
            'date_fin':                    date_fin_str,
            'ruptures':                    ruptures,
            'total_count':                 len(ruptures),
            'ruptures_semi_finis':         ruptures_semi_finis,
            'total_count_semi_finis':      len(ruptures_semi_finis),
            'ruptures_pour_semi_finis':    ruptures_pour_semi_finis,
            'total_count_pour_semi_finis': len(ruptures_pour_semi_finis),
            'ruptures_totaux':             ruptures_totaux,
            'total_count_totaux':          len(ruptures_totaux),
            'commandes_internes_periode':  commandes_internes_periode,
            'produits_list':               produits_avec_recette,
            'unite_choices':               Recette.UNITE_CHOICES,
            'recettes_json':               json.dumps(recettes_produit_unite_map()),
            'rupture_stock_url':           rupture_stock_url,
        })


def _calculer_ruptures_totaux(date_debut, date_fin):
    """Recalcule directement la synthèse de la Partie 4 (besoins combinés des
    parties 1 et 3, ruptures réelles uniquement — Manque > 0 — avec prix
    d'achat). Fonction autonome (recalcule tout depuis la base) réutilisée
    par RuptureStockPdfView, qui répond à une requête HTTP séparée de la
    page principale. Retourne (ruptures_totaux, erreurs)."""
    besoins, _besoins_semi_fini, erreurs = calculer_besoins_matieres_premieres_periode(date_debut, date_fin)
    besoins_pour_semi_finis, erreurs_semi_finis = calculer_besoins_matieres_premieres_pour_semi_finis_periode(
        date_debut, date_fin,
    )
    erreurs = erreurs + erreurs_semi_finis

    besoins_totaux = {}
    for mp_id, quantite in besoins.items():
        besoins_totaux[mp_id] = besoins_totaux.get(mp_id, Decimal('0')) + quantite
    for mp_id, quantite in besoins_pour_semi_finis.items():
        besoins_totaux[mp_id] = besoins_totaux.get(mp_id, Decimal('0')) + quantite

    derniers_achat_ids = (
        Achat.objects.values('achat_code_id').annotate(dernier_id=Max('id')).values_list('dernier_id', flat=True)
    )
    date_limite_60j = date.today() - timedelta(days=60)

    ruptures_totaux = [
        r for r in _construire_ruptures_matieres(besoins_totaux, derniers_achat_ids, date_limite_60j)
        if r['manque'] > 0
    ]
    return ruptures_totaux, erreurs


class RuptureStockPdfView(GroupRequiredMixin, View):
    """Génère le PDF « État avec dernier prix » ou « État avec meilleur prix
    (60 jours) » (paramètre GET `prix` = 'dernier' ou 'meilleur60j') à partir
    de la synthèse de la Partie 4 (_calculer_ruptures_totaux) : une ligne par
    matière première en rupture (quantité = Manque, prix selon le type
    choisi, Total ligne = quantité × prix), avec un total général en pied de
    tableau. Renvoyé en « inline » : le lecteur PDF du navigateur fournit
    nativement l'impression et le téléchargement."""
    group_required = 'Administration'

    def get(self, request):
        today = date.today()
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today
        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today + timedelta(days=6)
        if date_debut > date_fin:
            date_debut, date_fin = today, today + timedelta(days=6)

        type_prix = request.GET.get('prix', 'dernier').strip()
        if type_prix not in ('dernier', 'meilleur60j'):
            type_prix = 'dernier'

        ruptures_totaux, erreurs = _calculer_ruptures_totaux(date_debut, date_fin)

        lignes = []
        total_general = Decimal('0')
        for r in ruptures_totaux:
            if type_prix == 'dernier':
                prix_unitaire = r['dernier_prix']
                fournisseur = r['dernier_fournisseur']
                date_prix = r['dernier_date']
            else:
                prix_unitaire = r['meilleur_prix_60j']
                fournisseur = r['meilleur_fournisseur_60j']
                date_prix = r['meilleur_date_60j']
            total_ligne = None
            if prix_unitaire is not None:
                total_ligne = (r['manque'] * prix_unitaire).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                total_general += total_ligne
            lignes.append({
                'matiere_premiere': r['matiere_premiere'],
                'fournisseur':      fournisseur,
                'date_prix':        date_prix,
                'manque':           r['manque'],
                'prix_unitaire':    prix_unitaire,
                'total_ligne':      total_ligne,
            })

        titre = (
            "État des matières premières à acheter — Dernier prix" if type_prix == 'dernier'
            else "État des matières premières à acheter — Meilleur prix (60 derniers jours)"
        )

        html = render_to_string('administration/rupture_stock/pdf_etat.html', {
            'titre':          titre,
            'date_debut':     date_debut,
            'date_fin':       date_fin,
            'lignes':         lignes,
            'total_general':  total_general,
            'genere_le':      timezone.now(),
        })

        pdf_buffer = BytesIO()
        pisa_status = pisa.CreatePDF(html, dest=pdf_buffer)
        if pisa_status.err:
            messages.error(request, "Échec de la génération du PDF.")
            return redirect('administration:rupture-stock-list')

        log_audit(
            AuditAction.EXPORT_PDF, f'Export PDF rupture de stock ({type_prix})',
            table='MatierePremiere',
        )

        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        filename = f'rupture_stock_{type_prix}_{date_debut.isoformat()}_{date_fin.isoformat()}.pdf'
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


class RuptureStockCommandesPdfView(GroupRequiredMixin, View):
    """PDF « récapitulatif des commandes à produire » — commandes internes
    non achevées de la période, groupées par échéance (tri croissant). Une
    ligne par commande (produit + quantité à produire), suivie d'un tableau
    des matières premières et produits semi-finis de la Recette du produit,
    mis à l'échelle de la quantité à produire (un seul niveau — pas de
    développement récursif des semi-finis, contrairement aux parties 3/4).
    Portrait ; chaque bloc (ligne + tableau) évite d'être coupé entre deux
    pages (page-break-inside: avoid)."""
    group_required = 'Administration'

    def get(self, request):
        today = date.today()
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today
        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today + timedelta(days=6)
        if date_debut > date_fin:
            date_debut, date_fin = today, today + timedelta(days=6)

        commandes = (
            CommandeInterne.objects
            .filter(is_completed=False, livraison_at__gte=date_debut, livraison_at__lte=date_fin)
            .select_related('recette__produit')
            .order_by('livraison_at', 'recette__produit__nom')
        )

        groupes = []
        groupe_courant = None
        for ci in commandes:
            if groupe_courant is None or groupe_courant['date'] != ci.livraison_at:
                groupe_courant = {'date': ci.livraison_at, 'commandes': []}
                groupes.append(groupe_courant)

            recette = ci.recette
            quantite_a_produire = ci.quantite_commander or Decimal('0')
            lignes_ingredients = []
            if recette is not None and recette.quantite and recette.quantite > 0 and quantite_a_produire > 0:
                for ligne in RecetteDetaille.objects.filter(recette=recette).select_related('matiere_premiere'):
                    if not ligne.quantite:
                        continue
                    quantite_utilisee = (
                        quantite_a_produire * ligne.quantite / recette.quantite
                    ).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                    lignes_ingredients.append({
                        'nom':      ligne.matiere_premiere.nom,
                        'unite':    ligne.matiere_premiere.unite,
                        'quantite': quantite_utilisee,
                    })

            groupe_courant['commandes'].append({
                'produit':             recette.produit if recette else None,
                'quantite_a_produire': quantite_a_produire,
                'unite':               recette.unite if recette else '',
                'lignes_ingredients':  lignes_ingredients,
            })

        html = render_to_string('administration/rupture_stock/pdf_commandes.html', {
            'date_debut': date_debut,
            'date_fin':   date_fin,
            'groupes':    groupes,
            'genere_le':  timezone.now(),
        })

        pdf_buffer = BytesIO()
        pisa_status = pisa.CreatePDF(html, dest=pdf_buffer)
        if pisa_status.err:
            messages.error(request, "Échec de la génération du PDF.")
            return redirect('administration:rupture-stock-list')

        log_audit(AuditAction.EXPORT_PDF, 'Export PDF commandes à produire', table='CommandeInterne')

        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        filename = f'commandes_a_produire_{date_debut.isoformat()}_{date_fin.isoformat()}.pdf'
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


# ─── Coût de production ─────────────────────────────────────────────────────

def _dernier_prix_matiere_premiere(mp_id, derniers_achat_ids):
    """Dernier prix unitaire d'achat pour une matière première — le plus
    récent achat_code.achat_at parmi les Achat non supersédés (voir
    derniers_achat_ids, même principe que _construire_ruptures_matieres) —
    ou None si cette matière première n'a jamais été achetée."""
    dernier = (
        AchatDetaille.objects
        .filter(matiere_premiere_id=mp_id, achat_id__in=derniers_achat_ids)
        .order_by('-achat__achat_code__achat_at', '-achat_id')
        .first()
    )
    return dernier.prix_unitaire if dernier else None


def _calculer_cout_unitaire(recette, prix_cache, derniers_achat_ids):
    """Coût de production unitaire d'une Recette : développe la recette en
    matières premières de base (resoudre_ingredients_recette) et valorise
    chacune à son dernier prix d'achat connu (prix_cache mémoïse ces prix
    entre appels, voir _dernier_prix_matiere_premiere). None si `recette` est
    absente ou sans quantité de référence valide (calcul impossible)."""
    if recette is None or not recette.quantite or recette.quantite <= 0:
        return None

    ingredients, _erreurs = resoudre_ingredients_recette(recette)

    cout_total = Decimal('0')
    for mp_id, quantite in ingredients.items():
        if mp_id not in prix_cache:
            prix_cache[mp_id] = _dernier_prix_matiere_premiere(mp_id, derniers_achat_ids)
        prix = prix_cache[mp_id]
        if prix is not None:
            cout_total += prix * quantite

    return (cout_total / recette.quantite).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


class CoutProductionListView(GroupRequiredMixin, View):
    """« Coût unitaire de production des produits actualisés en fonction des
    prix d'achats récents des matières premières, basé sur la recette
    standard » : pour chaque Recette de produit FINI (is_produit_semi_fini=
    False), développe récursivement sa recette jusqu'aux matières premières
    de base (voir production.services.resoudre_ingredients_recette — gère
    aussi le cas où un ingrédient est lui-même un produit semi-fini, avec
    protection anti-cycle) et calcule le coût de production unitaire à
    partir du dernier prix d'achat de chaque matière première de base.

    Coût de production unitaire = (Σ dernier_prix_mp × quantité_mp nécessaire
    selon la recette développée) / quantité de référence de la recette du
    produit fini. Les recettes sans quantité de référence valide (≤ 0) sont
    exclues du rapport (calcul impossible).

    Colonnes « maximisées » : parmi les CommandeInterne de la recette, on
    retient celle dont l'indice de maximisation (quantite_commander /
    quantite_produite) est LE PLUS FAIBLE — c'est la commande interne qui, si
    elle était reproduite, minimiserait le coût de production du produit fini
    (Coût unitaire maximisé = Coût de production unitaire × cet indice).
    CommandeInterne sans quantite_commander/quantite_produite exploitable
    ignorées ; si aucune n'est exploitable, les 5 colonnes restent vides."""
    group_required = 'Administration'
    template_name = 'administration/production/cout_production_list.html'
    PAGINATE_BY = 10

    def get(self, request):
        derniers_achat_ids = (
            Achat.objects.values('achat_code_id').annotate(dernier_id=Max('id')).values_list('dernier_id', flat=True)
        )

        recettes = (
            Recette.objects
            .filter(produit__is_produit_semi_fini=False)
            .select_related('produit')
            .prefetch_related('commandes_internes')
            .order_by('produit__nom')
        )

        prix_cache = {}

        lignes = []
        for recette in recettes:
            cout_unitaire = _calculer_cout_unitaire(recette, prix_cache, derniers_achat_ids)
            if cout_unitaire is None:
                continue

            prix_unitaire = recette.produit.prix_unitaire or Decimal('0')
            benefice_unitaire = prix_unitaire - cout_unitaire

            # ── Commande interne minimisant l'indice de maximisation ────────
            commande_optimale = None
            indice_min = None
            for ci in recette.commandes_internes.all():
                if not ci.quantite_commander or not ci.quantite_produite:
                    continue
                indice = ci.quantite_commander / ci.quantite_produite
                if indice_min is None or indice < indice_min:
                    indice_min = indice
                    commande_optimale = ci

            if commande_optimale is not None:
                qte_commandee_max = commande_optimale.quantite_commander
                qte_produite_max = commande_optimale.quantite_produite
                cout_unitaire_max = (cout_unitaire * indice_min).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
                benefice_max = prix_unitaire - cout_unitaire_max
            else:
                qte_commandee_max = None
                qte_produite_max = None
                indice_min = None
                cout_unitaire_max = None
                benefice_max = None

            lignes.append({
                'produit':            recette.produit,
                'prix_unitaire':      prix_unitaire,
                'cout_unitaire':      cout_unitaire,
                'benefice_unitaire':  benefice_unitaire,
                'qte_commandee_max':  qte_commandee_max,
                'qte_produite_max':   qte_produite_max,
                'indice_max':         indice_min,
                'cout_unitaire_max':  cout_unitaire_max,
                'benefice_max':       benefice_max,
            })

        paginator = Paginator(lignes, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':     page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'lignes':       page_obj.object_list,
            'total_count':  paginator.count,
        })


# ─── Chiffre d'affaire par produit ───────────────────────────────────────────

def _construire_lignes_chiffre_affaire_produit(date_debut, date_fin):
    """Construit les lignes de chiffre d'affaires par produit sur
    [date_debut, date_fin] et les totaux de la période — logique partagée par
    ChiffreAffaireParProduitView et ChiffreAffaireProduitGraphiquePdfView.

    Pour chaque Produit vendu (BonLivraisonDetaille.produit) entre
    `date_debut` et `date_fin` (BonLivraisonCode.bon_livraison_at), en ne
    retenant que le DERNIER BonLivraison de chaque BonLivraisonCode
    (dernier_bl_id_par_code — même principe que
    commercial.views.BonLivraisonListView : un BonLivraisonCode révisé
    plusieurs fois ne doit être compté qu'une seule fois, via son
    BonLivraison le plus récent) :

      Total quantité   = Σ quantite
      Total montant    = Σ total_ligne
      Part du CA       = Total montant / Total chiffre d'affaire (période)
      Coût unitaire    = coût de production du produit, calculé indépendamment
                         de la période (recette standard actualisée avec les
                         derniers prix d'achat — voir _calculer_cout_unitaire /
                         production.services.resoudre_ingredients_recette)
      Bénéfice         = Total montant − (Total quantité × Coût unitaire)
      Total bénéfice brut = Σ Bénéfice de tous les produits de la période
      Part du bénéfice = Bénéfice / Total bénéfice brut (période)

    Coût unitaire, Bénéfice et Part du bénéfice sont vides (None) pour un
    produit sans recette exploitable (calcul de coût impossible) ; ce produit
    ne contribue alors pas au Total bénéfice brut.

    Retourne (lignes, total_chiffre_affaire, total_benefice_brut)."""
    dernier_bl_id_par_code = (
        BonLivraison.objects
        .filter(
            bon_livraison_code__bon_livraison_at__gte=date_debut,
            bon_livraison_code__bon_livraison_at__lte=date_fin,
        )
        .values('bon_livraison_code_id')
        .annotate(dernier_id=Max('id'))
        .values_list('dernier_id', flat=True)
    )

    groupes = list(
        BonLivraisonDetaille.objects
        .filter(bon_livraison_id__in=dernier_bl_id_par_code, produit__isnull=False)
        .values('produit_id', 'produit__nom')
        .annotate(total_qte=Sum('quantite'), total_montant=Sum('total_ligne'))
        .order_by('-total_montant')
    )

    total_chiffre_affaire = Decimal('0')
    for g in groupes:
        total_chiffre_affaire += g['total_montant'] or Decimal('0')

    derniers_achat_ids = (
        Achat.objects.values('achat_code_id').annotate(dernier_id=Max('id')).values_list('dernier_id', flat=True)
    )
    recette_par_produit = {
        r.produit_id: r
        for r in Recette.objects.filter(produit_id__in=[g['produit_id'] for g in groupes])
    }
    prix_cache = {}

    lignes = []
    total_benefice_brut = Decimal('0')
    for g in groupes:
        total_qte = g['total_qte'] or Decimal('0')
        total_montant = g['total_montant'] or Decimal('0')
        cout_unitaire = _calculer_cout_unitaire(
            recette_par_produit.get(g['produit_id']), prix_cache, derniers_achat_ids,
        )
        if cout_unitaire is not None:
            benefice = total_montant - (total_qte * cout_unitaire)
            total_benefice_brut += benefice
        else:
            benefice = None

        lignes.append({
            'produit_nom':   g['produit__nom'],
            'total_qte':     total_qte,
            'total_montant': total_montant,
            'cout_unitaire': cout_unitaire,
            'benefice':      benefice,
        })

    for l in lignes:
        l['part_ca'] = (
            (l['total_montant'] / total_chiffre_affaire * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if total_chiffre_affaire else None
        )
        l['part_benefice'] = (
            (l['benefice'] / total_benefice_brut * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if l['benefice'] is not None and total_benefice_brut else None
        )

    return lignes, total_chiffre_affaire, total_benefice_brut


def _generer_graphique_barres(labels, valeurs, titre, couleur, suffixe):
    """Génère un graphique en barres horizontal (une barre par élément de
    `labels`/`valeurs`) via matplotlib et retourne le PNG encodé en base64,
    prêt à être intégré dans un template PDF via
    <img src="data:image/png;base64,...">. `suffixe` est ajouté après chaque
    valeur affichée au bout de sa barre (ex. ' %')."""
    fig, ax = plt.subplots(figsize=(11, max(3, 0.4 * len(labels) + 1)))
    positions = range(len(labels))
    ax.barh(list(positions), valeurs, color=couleur)
    ax.set_yticks(list(positions))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel(titre, fontsize=9)
    ax.set_title(titre, fontsize=11, fontweight='bold')

    for i, v in enumerate(valeurs):
        ax.text(v, i, f' {v:,.2f}{suffixe}'.replace(',', ' '), va='center', fontsize=7)

    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format='png', dpi=150)
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


_GRAPHIQUES_CHIFFRE_AFFAIRE_PRODUIT = {
    'montant-total': {
        'champ':   'total_montant',
        'titre':   "Montant total par produit (TND)",
        'couleur': '#4e79a7',
        'suffixe': '',
    },
    'part-ca': {
        'champ':   'part_ca',
        'titre':   "Part du chiffre d'affaire par produit (%)",
        'couleur': '#59a14f',
        'suffixe': ' %',
    },
    'benefice': {
        'champ':   'benefice',
        'titre':   "Bénéfice par produit (TND)",
        'couleur': '#f28e2b',
        'suffixe': '',
    },
    'part-benefice': {
        'champ':   'part_benefice',
        'titre':   "Part du bénéfice par produit (%)",
        'couleur': '#e15759',
        'suffixe': ' %',
    },
}


class ChiffreAffaireParProduitView(GroupRequiredMixin, View):
    """Chiffre d'affaires par produit sur une période — voir
    _construire_lignes_chiffre_affaire_produit pour le détail du calcul de
    chaque colonne. Vue paginée (10 produits par page)."""
    group_required = 'Administration'
    template_name = 'administration/production/chiffre_affaire_produit_list.html'
    PAGINATE_BY = 10

    def get(self, request):
        today = date.today()
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()
        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        lignes, total_chiffre_affaire, total_benefice_brut = _construire_lignes_chiffre_affaire_produit(
            date_debut, date_fin,
        )

        paginator = Paginator(lignes, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':              page_obj,
            'is_paginated':          page_obj.has_other_pages(),
            'lignes':                page_obj.object_list,
            'total_count':           paginator.count,
            'date_debut':            date_debut_str,
            'date_fin':              date_fin_str,
            'total_chiffre_affaire': total_chiffre_affaire,
            'total_benefice_brut':   total_benefice_brut,
        })


class ChiffreAffaireProduitGraphiquePdfView(GroupRequiredMixin, View):
    """Export PDF d'un graphique en barres horizontal représentant une
    colonne du rapport « Chiffre d'affaires par produit » (voir
    _construire_lignes_chiffre_affaire_produit) sur la période choisie — sans
    pagination : le graphique couvre tous les produits de la période. Le
    kwarg d'URL `champ` sélectionne la colonne parmi
    _GRAPHIQUES_CHIFFRE_AFFAIRE_PRODUIT (montant-total, part-ca, benefice,
    part-benefice). Les produits pour lesquels cette colonne est vide (« — »,
    calcul de coût impossible) sont omis du graphique."""
    group_required = 'Administration'
    template_name = 'administration/production/pdf_graphique_chiffre_affaire.html'

    def get(self, request, champ):
        config = _GRAPHIQUES_CHIFFRE_AFFAIRE_PRODUIT.get(champ)
        if config is None:
            messages.error(request, "Graphique demandé inconnu.")
            return redirect('administration:chiffre-affaire-produit-list')

        today = date.today()
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today

        lignes, _total_ca, _total_benefice = _construire_lignes_chiffre_affaire_produit(date_debut, date_fin)
        donnees = [(l['produit_nom'], l[config['champ']]) for l in lignes if l[config['champ']] is not None]

        if not donnees:
            messages.error(request, "Aucune donnée à représenter sur cette période.")
            return redirect(
                f"{reverse('administration:chiffre-affaire-produit-list')}"
                f"?date_debut={date_debut.isoformat()}&date_fin={date_fin.isoformat()}"
            )

        labels = [nom for nom, _valeur in donnees]
        valeurs = [float(valeur) for _nom, valeur in donnees]
        chart_b64 = _generer_graphique_barres(labels, valeurs, config['titre'], config['couleur'], config['suffixe'])

        html = render_to_string(self.template_name, {
            'titre':      config['titre'],
            'date_debut': date_debut,
            'date_fin':   date_fin,
            'chart_b64':  chart_b64,
            'genere_le':  timezone.now(),
        })

        pdf_buffer = BytesIO()
        pisa_status = pisa.CreatePDF(html, dest=pdf_buffer)
        if pisa_status.err:
            messages.error(request, "Échec de la génération du PDF.")
            return redirect('administration:chiffre-affaire-produit-list')

        log_audit(
            AuditAction.EXPORT_PDF, f"Export PDF graphique « {config['titre']} »",
            table='BonLivraisonDetaille',
        )

        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        filename = f'graphique_{champ}_{date_debut.isoformat()}_{date_fin.isoformat()}.pdf'
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


# ─── Chiffre d'affaire par commercial ────────────────────────────────────────

def _generer_graphique_barres_3d(labels, valeurs, titre):
    """Génère un graphique en barres 3D (une barre par élément de
    `labels`/`valeurs`) via matplotlib (mpl_toolkits.mplot3d) et retourne le
    PNG encodé en base64, prêt à être intégré directement dans une page HTML
    via <img src="data:image/png;base64,...">."""
    fig = plt.figure(figsize=(9, 6))
    ax = fig.add_subplot(111, projection='3d')

    couleurs = plt.get_cmap('tab10').colors
    positions = list(range(len(labels)))
    bar_colors = [couleurs[i % len(couleurs)] for i in positions]

    ax.bar3d(
        positions, [0] * len(labels), [0] * len(labels),
        [0.6] * len(labels), [0.6] * len(labels), valeurs,
        color=bar_colors, shade=True,
    )
    ax.set_xticks([p + 0.3 for p in positions])
    ax.set_xticklabels(labels, rotation=20, ha='right', fontsize=7)
    ax.set_yticks([])
    ax.set_zlabel('TND', fontsize=8)
    ax.set_title(titre, fontsize=11, fontweight='bold')
    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format='png', dpi=150)
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def _generer_graphique_camembert(labels, valeurs, titre):
    """Génère un graphique circulaire 2D (camembert) via matplotlib et
    retourne le PNG encodé en base64, prêt à être intégré directement dans
    une page HTML via <img src="data:image/png;base64,...">."""
    fig, ax = plt.subplots(figsize=(7, 6))
    couleurs = plt.get_cmap('tab10').colors

    ax.pie(
        valeurs, labels=labels, autopct='%1.1f%%', startangle=90,
        colors=[couleurs[i % len(couleurs)] for i in range(len(labels))],
        textprops={'fontsize': 8},
    )
    ax.axis('equal')
    ax.set_title(titre, fontsize=11, fontweight='bold')
    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format='png', dpi=150)
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


class ChiffreAffaireParCommercialView(GroupRequiredMixin, View):
    """Chiffre d'affaires par commercial sur une période : pour chaque
    utilisateur (BonLivraison.user) ayant émis un BonLivraison entre
    `date_debut` et `date_fin` (BonLivraisonCode.bon_livraison_at), en ne
    retenant que le DERNIER BonLivraison de chaque BonLivraisonCode (même
    principe que ChiffreAffaireParProduitView / commercial.views.
    BonLivraisonListView : un BonLivraisonCode révisé plusieurs fois ne doit
    être compté qu'une seule fois, via son BonLivraison le plus récent) :

      Total montant = Σ total_montant (BonLivraison)
      Part du CA    = Total montant / Total chiffre d'affaire (période)

    Total chiffre d'affaire = Σ total_montant de tous les BonLivraison
    retenus (derniers de chaque BonLivraisonCode) de la période.

    Sous le tableau (paginé, 4 lignes/page) : un graphique en barres 3D du
    Total montant par commercial, et un graphique circulaire (camembert) 2D
    de la Part du chiffre d'affaire — tous deux calculés sur l'ensemble des
    commerciaux de la période, indépendamment de la pagination."""
    group_required = 'Administration'
    template_name = 'administration/production/chiffre_affaire_commercial_list.html'
    PAGINATE_BY = 4

    def get(self, request):
        today = date.today()
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()
        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        dernier_bl_id_par_code = (
            BonLivraison.objects
            .filter(
                bon_livraison_code__bon_livraison_at__gte=date_debut,
                bon_livraison_code__bon_livraison_at__lte=date_fin,
            )
            .values('bon_livraison_code_id')
            .annotate(dernier_id=Max('id'))
            .values_list('dernier_id', flat=True)
        )

        groupes = list(
            BonLivraison.objects
            .filter(pk__in=dernier_bl_id_par_code)
            .values('user_id')
            .annotate(total_montant=Sum('total_montant'))
            .order_by('-total_montant')
        )

        total_chiffre_affaire = Decimal('0')
        for g in groupes:
            total_chiffre_affaire += g['total_montant'] or Decimal('0')

        users_par_id = {u.pk: u for u in User.objects.filter(pk__in=[g['user_id'] for g in groupes])}

        lignes = []
        for g in groupes:
            total_montant = g['total_montant'] or Decimal('0')
            lignes.append({
                'commercial':    users_par_id.get(g['user_id']),
                'total_montant': total_montant,
                'part_ca': (
                    (total_montant / total_chiffre_affaire * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    if total_chiffre_affaire else None
                ),
            })

        chart_montant_b64 = None
        chart_part_b64 = None
        if lignes:
            labels = [
                (l['commercial'].get_full_name() or l['commercial'].username) if l['commercial'] else '—'
                for l in lignes
            ]
            chart_montant_b64 = _generer_graphique_barres_3d(
                labels, [float(l['total_montant']) for l in lignes],
                "Chiffre d'affaire par commercial (TND)",
            )
            if total_chiffre_affaire:
                chart_part_b64 = _generer_graphique_camembert(
                    labels, [float(l['part_ca']) for l in lignes],
                    "Part du chiffre d'affaire par commercial (%)",
                )

        paginator = Paginator(lignes, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':              page_obj,
            'is_paginated':          page_obj.has_other_pages(),
            'lignes':                page_obj.object_list,
            'total_count':           paginator.count,
            'date_debut':            date_debut_str,
            'date_fin':              date_fin_str,
            'total_chiffre_affaire': total_chiffre_affaire,
            'chart_montant_b64':     chart_montant_b64,
            'chart_part_b64':        chart_part_b64,
        })


# ─── Chiffre d'affaire par zone ──────────────────────────────────────────────

def _generer_graphique_camembert_3d(labels, valeurs, titre):
    """Génère un graphique circulaire en 3D (camembert « épais », extrudé le
    long de l'axe z via des parois Poly3DCollection) via matplotlib et
    retourne le PNG encodé en base64, prêt à être intégré dans une pop-up via
    <img src="data:image/png;base64,...">."""
    total = sum(valeurs)
    couleurs = plt.get_cmap('tab10').colors
    hauteur = 0.35
    n_segments = 24

    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(111, projection='3d')

    angle_courant = 0.0
    for i, valeur in enumerate(valeurs):
        angle_part = (valeur / total) * 2 * np.pi if total else 0
        theta = np.linspace(angle_courant, angle_courant + angle_part, n_segments)
        couleur = couleurs[i % len(couleurs)]

        x = np.concatenate(([0], np.cos(theta)))
        y = np.concatenate(([0], np.sin(theta)))
        ax.add_collection3d(Poly3DCollection(
            [list(zip(x, y, [hauteur] * len(x)))], facecolor=couleur, edgecolor='white', linewidths=0.3,
        ))
        ax.add_collection3d(Poly3DCollection(
            [list(zip(x, y, [0] * len(x)))], facecolor=couleur, edgecolor='white', linewidths=0.3,
        ))

        for j in range(len(theta) - 1):
            x0, y0 = np.cos(theta[j]), np.sin(theta[j])
            x1, y1 = np.cos(theta[j + 1]), np.sin(theta[j + 1])
            paroi = [[(x0, y0, 0), (x1, y1, 0), (x1, y1, hauteur), (x0, y0, hauteur)]]
            ax.add_collection3d(Poly3DCollection(paroi, facecolor=couleur, edgecolor='white', linewidths=0.1))

        angle_courant += angle_part

    ax.set_xlim(-1, 1)
    ax.set_ylim(-1, 1)
    ax.set_zlim(0, hauteur * 2)
    ax.set_axis_off()
    ax.view_init(elev=35, azim=-60)
    ax.set_title(titre, fontsize=11, fontweight='bold')

    handles = [plt.Rectangle((0, 0), 1, 1, color=couleurs[i % len(couleurs)]) for i in range(len(labels))]
    legendes = [
        f'{label} — {(valeur / total * 100):.1f} %' if total else label
        for label, valeur in zip(labels, valeurs)
    ]
    ax.legend(handles, legendes, loc='center left', bbox_to_anchor=(1.0, 0.5), fontsize=8)

    buffer = BytesIO()
    fig.savefig(buffer, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


class ChiffreAffaireParZoneView(GroupRequiredMixin, View):
    """Chiffre d'affaires par zone sur une période : pour chaque Zone
    (Client.zone, via BonLivraisonCode.client) ayant reçu un BonLivraison
    entre `date_debut` et `date_fin` (BonLivraisonCode.bon_livraison_at), en
    ne retenant que le DERNIER BonLivraison de chaque BonLivraisonCode (même
    principe que ChiffreAffaireParProduitView / commercial.views.
    BonLivraisonListView : un BonLivraisonCode révisé plusieurs fois ne doit
    être compté qu'une seule fois, via son BonLivraison le plus récent) :

      Total montant = Σ total_montant (BonLivraison)
      Part du CA    = Total montant / Total chiffre d'affaire (période)

    Total chiffre d'affaire = Σ total_montant de tous les BonLivraison
    retenus (derniers de chaque BonLivraisonCode) de la période. Les clients
    sans zone assignée (Client.zone nullable) sont regroupés sous « Sans
    zone ».

    Sous le tableau (paginé, 4 lignes/page) : deux boutons ouvrent chacun une
    pop-up avec un graphique en 3D — barres pour le Total montant, camembert
    pour la Part du chiffre d'affaire — calculés sur l'ensemble des zones de
    la période, indépendamment de la pagination."""
    group_required = 'Administration'
    template_name = 'administration/production/chiffre_affaire_zone_list.html'
    PAGINATE_BY = 4

    def get(self, request):
        today = date.today()
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str = request.GET.get('date_fin', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()
        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        dernier_bl_id_par_code = (
            BonLivraison.objects
            .filter(
                bon_livraison_code__bon_livraison_at__gte=date_debut,
                bon_livraison_code__bon_livraison_at__lte=date_fin,
            )
            .values('bon_livraison_code_id')
            .annotate(dernier_id=Max('id'))
            .values_list('dernier_id', flat=True)
        )

        groupes = list(
            BonLivraison.objects
            .filter(pk__in=dernier_bl_id_par_code)
            .values('bon_livraison_code__client__zone_id', 'bon_livraison_code__client__zone__nom')
            .annotate(total_montant=Sum('total_montant'))
            .order_by('-total_montant')
        )

        total_chiffre_affaire = Decimal('0')
        for g in groupes:
            total_chiffre_affaire += g['total_montant'] or Decimal('0')

        lignes = []
        for g in groupes:
            total_montant = g['total_montant'] or Decimal('0')
            lignes.append({
                'zone_nom':      g['bon_livraison_code__client__zone__nom'] or 'Sans zone',
                'total_montant': total_montant,
                'part_ca': (
                    (total_montant / total_chiffre_affaire * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    if total_chiffre_affaire else None
                ),
            })

        chart_montant_b64 = None
        chart_part_b64 = None
        if lignes:
            labels = [l['zone_nom'] for l in lignes]
            chart_montant_b64 = _generer_graphique_barres_3d(
                labels, [float(l['total_montant']) for l in lignes],
                "Chiffre d'affaire par zone (TND)",
            )
            if total_chiffre_affaire:
                chart_part_b64 = _generer_graphique_camembert_3d(
                    labels, [float(l['total_montant']) for l in lignes],
                    "Part du chiffre d'affaire par zone (%)",
                )

        paginator = Paginator(lignes, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':              page_obj,
            'is_paginated':          page_obj.has_other_pages(),
            'lignes':                page_obj.object_list,
            'total_count':           paginator.count,
            'date_debut':            date_debut_str,
            'date_fin':              date_fin_str,
            'total_chiffre_affaire': total_chiffre_affaire,
            'chart_montant_b64':     chart_montant_b64,
            'chart_part_b64':        chart_part_b64,
        })


# ─── Solde compte caisse ─────────────────────────────────────────────────────

LIBELLE_MISE_A_JOUR_SOLDE = 'Mise à jour solde compte'


class HistoriqueSoldeCompteListView(GroupRequiredMixin, View):
    """Page « Liste des soldes comptes caisses des Employés » : Section 1
    (filtres — Date début par défaut aujourd'hui - 6 jours, Date fin par
    défaut aujourd'hui, Employé : liste déroulante des utilisateurs actifs
    des groupes Administration, Production et Commercial — à l'exclusion
    des superusers —, « — Tous les users — » par défaut). Section 2 :
    tableau des HistoriqueSoldeCompte filtrés sur solde_at dans [Date
    début, Date fin] et, si renseigné, sur Employé, trié par date de solde
    décroissante, paginé (10 lignes), tous les champs verrouillés (Date,
    Employé, Solde caisse calculé, Solde caisse réel — en rouge si
    différent du calculé —, Observation) ; le champ Administration (admin)
    n'est pas affiché dans le tableau. Le bouton « + Ajout d'une mise à jour
    du solde compte caisse » ouvre administration/solde-caisse/.
    « Visualiser » ouvre HistoriqueSoldeCompteTransactionsView (réservée au
    groupe Administration)."""
    group_required = 'Administration'
    template_name = 'administration/historique_solde_compte/list.html'
    PAGINATE_BY = 10

    def get(self, request):
        today = date.today()

        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin', '').strip()
        user_id        = request.GET.get('user_id', '').strip()

        try:
            date_debut = date.fromisoformat(date_debut_str)
        except ValueError:
            date_debut = today - timedelta(days=6)
            date_debut_str = date_debut.isoformat()

        try:
            date_fin = date.fromisoformat(date_fin_str)
        except ValueError:
            date_fin = today
            date_fin_str = date_fin.isoformat()

        qs = HistoriqueSoldeCompte.objects.select_related('user').filter(
            solde_at__gte=date_debut, solde_at__lte=date_fin,
        )
        if user_id:
            qs = qs.filter(user_id=user_id)
        qs = qs.order_by('-solde_at', '-pk')

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        users_list = (
            User.objects
            .filter(is_active=True, groups__name__in=['Administration', 'Commercial', 'Production'])
            .exclude(is_superuser=True)
            .distinct()
            .order_by('last_name', 'first_name')
        )

        return render(request, self.template_name, {
            'page_obj':     page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'lignes':       page_obj.object_list,
            'total_count':  paginator.count,
            'date_debut':   date_debut_str,
            'date_fin':     date_fin_str,
            'user_id':      user_id,
            'users_list':   users_list,
        })


class HistoriqueSoldeCompteTransactionsView(GroupRequiredMixin, View):
    """Page « Liste des transactions caisse liées au solde compte caisse
    sélectionné », réservée au groupe Administration — ouverte par le
    bouton « Visualiser » de administration/soldes-caisse/. Section 1
    (« Données solde caisse ») : Employé, Date, Solde caisse calculé, Solde
    caisse réel, Observation de l'HistoriqueSoldeCompte sélectionné (pk),
    tous les champs verrouillés, sur une seule ligne. Section 2 : mêmes
    colonnes/gabarit que administration/transactions/ (sans la Section 1 de
    filtres de cette dernière), pour les Transaction dont user_id=Employé,
    created_at &lt;= Date et hist_solde_compte_id=pk (par construction, ces
    trois conditions décrivent le même ensemble — celui des transactions
    soldées par cet enregistrement). Un bouton « Page précédente » en bas à
    droite du tableau ramène à la page précédente (history.back())."""
    group_required = 'Administration'
    template_name = 'administration/historique_solde_compte/transactions.html'
    PAGINATE_BY = 25

    def get(self, request, pk):
        hist = get_object_or_404(HistoriqueSoldeCompte.objects.select_related('user'), pk=pk)

        qs = (
            TransactionModel.objects
            .select_related('user', 'libelle')
            .filter(user_id=hist.user_id, created_at__date__lte=hist.solde_at, hist_solde_compte_id=hist.pk)
            .order_by('-created_at', '-pk')
        )

        totaux = qs.aggregate(
            credit=Sum('montant', filter=Q(montant__gte=0)),
            debit=Sum('montant', filter=Q(montant__lt=0)),
        )
        totaux['net'] = (totaux['credit'] or 0) + (totaux['debit'] or 0)

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'hist':         hist,
            'page_obj':     page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'total_count':  paginator.count,
            'totaux':       totaux,
        })


class SoldeCaisseView(GroupRequiredMixin, View):
    """« Mise à jour du solde compte caisse » : pour l'employé sélectionné en
    Section 1 (tous les utilisateurs actifs sauf l'utilisateur connecté),
    liste en Section 2 (paginée, 10 lignes) les Transaction non encore
    soldées (hist_solde_compte IS NULL) dont created_at &lt;= solde_at
    (aujourd'hui, verrouillé), par ordre croissant. La Section 3 en calcule
    la somme (calcul_solde, sur l'ensemble filtré, indépendamment de la
    pagination) et propose une correction manuelle (correction_solde) et une
    observation.

    Le bouton « Enregistrer » (voir _post_enregistrer_solde) valide, dans
    l'ordre : Date de la Section 1 = aujourd'hui ; Employé défini ; la
    Section 2 contient au moins un enregistrement dont le libellé n'est pas
    « Mise à jour solde compte » (c.-à-d. au moins une transaction réellement
    nouvelle, pas seulement le solde de report précédent) ; Solde réel >= 0.
    Si tout est valide, dans une transaction.atomic() :
    1) création d'un HistoriqueSoldeCompte (solde_at, calcul_solde —
       recalculé côté serveur, jamais depuis une valeur postée —,
       correction_solde, observation, admin=utilisateur connecté,
       user=Employé) ; 2) création d'une nouvelle Transaction « table_id =
       historiquesoldecompte_id_&lt;id du HistoriqueSoldeCompte&gt; », montant =
       Solde réel, libellé « Mise à jour solde compte » (get_or_create),
       hist_solde_compte=Null (nouveau point de départ non soldé) ; 3) les
       enregistrements de la Section 2 capturés AVANT l'étape 2 (pour ne
       jamais y inclure la transaction tout juste créée) sont marqués
       soldés (hist_solde_compte = ce nouvel HistoriqueSoldeCompte). En cas
       d'échec de validation, la page est régénérée avec les valeurs
       saisies (correction_solde, observation) préservées.

    La Section 4 ajoute un Transfere dont le compte source et le compte
    destinataire sont choisis parmi les utilisateurs actifs des groupes
    Administration, Commercial et Production, avec deux contraintes :
    - le compte destinataire exclut l'utilisateur déjà choisi comme compte
      source (et vice versa), pour qu'ils soient toujours différents ;
    - l'un des deux (compte source OU compte destinataire) doit
      obligatoirement être l'employé sélectionné en Section 1 — dès que
      l'un des deux champs est fixé sur un utilisateur différent de cet
      employé, l'autre est automatiquement forcé sur cet employé (JS pour
      l'ergonomie, revalidé côté serveur en dernier recours). La Section 4
      n'est donc utilisable que si un employé est sélectionné en Section 1.

    Le bouton « + Ajouter » applique par ailleurs la même logique
    d'enregistrement que TransfereCreateView (administration/transferts/
    nouveau/) — validation via Transfere.clean() (montant strictement
    positif, comptes différents), sauvegarde atomique puis
    creer_transactions(), même audit — avec une validation supplémentaire
    propre à cette page : la date du transfert ne peut pas être postérieure
    à aujourd'hui. En cas de succès, la page est rechargée (GET) avec le
    même employé sélectionné en Section 1."""
    group_required = 'Administration'
    template_name = 'administration/solde_caisse/form.html'
    PAGINATE_BY = 10

    def _employes_list(self, request):
        return User.objects.filter(is_active=True).exclude(pk=request.user.pk).order_by('last_name', 'first_name')

    def _comptes_users_list(self):
        return (
            User.objects
            .filter(is_active=True, groups__name__in=['Administration', 'Commercial', 'Production'])
            .distinct()
            .order_by('last_name', 'first_name')
        )

    def _transactions_non_soldees(self, compte_employe, solde_at):
        return (
            TransactionModel.objects
            .filter(user=compte_employe, hist_solde_compte__isnull=True, created_at__date__lte=solde_at)
            .select_related('libelle')
            .order_by('created_at')
        )

    def _build_context(self, request, *, user_id, page, transfere_overrides=None, solde_overrides=None):
        solde_at = date.today()

        compte_employe = None
        if user_id and user_id.isdigit():
            compte_employe = User.objects.filter(pk=user_id, is_active=True).exclude(pk=request.user.pk).first()

        if compte_employe is not None:
            transactions_qs = self._transactions_non_soldees(compte_employe, solde_at)
            calcul_solde = transactions_qs.aggregate(total=Sum('montant'))['total'] or Decimal('0')
        else:
            transactions_qs = TransactionModel.objects.none()
            calcul_solde = Decimal('0')

        paginator = Paginator(transactions_qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(page)

        transfere_defaults = {
            'transfere_at':          solde_at.isoformat(),
            'compte_source_id':      '',
            'libelle':               '',
            'compte_destination_id': '',
            'montant':               '0.000',
        }
        if transfere_overrides:
            transfere_defaults.update(transfere_overrides)

        solde_defaults = {
            'correction_solde': '',
            'observation':      '',
        }
        if solde_overrides:
            solde_defaults.update(solde_overrides)

        return {
            'solde_at':           solde_at,
            'employes_list':      self._employes_list(request),
            'user_id':            user_id or '',
            'comptes_users_list': self._comptes_users_list(),
            'page_obj':           page_obj,
            'is_paginated':       page_obj.has_other_pages(),
            'lignes':             page_obj.object_list,
            'total_count':        paginator.count,
            'calcul_solde':       calcul_solde,
            **transfere_defaults,
            **solde_defaults,
        }

    def get(self, request):
        user_id = request.GET.get('user_id', '').strip()
        page = request.GET.get('page', 1)
        return render(request, self.template_name, self._build_context(request, user_id=user_id, page=page))

    def post(self, request):
        if request.POST.get('action') == 'enregistrer_solde':
            return self._post_enregistrer_solde(request)
        return self._post_ajouter_transfert(request)

    def _post_enregistrer_solde(self, request):
        user_id           = request.POST.get('user_id', '').strip()
        solde_at_str       = request.POST.get('solde_at', '').strip()
        correction_solde_s = request.POST.get('correction_solde', '').strip()
        observation        = request.POST.get('observation', '').strip()

        def _echec(message):
            messages.error(request, message)
            context = self._build_context(
                request, user_id=user_id, page=1,
                solde_overrides={
                    'correction_solde': correction_solde_s,
                    'observation':      observation,
                },
            )
            return render(request, self.template_name, context)

        try:
            solde_at = date.fromisoformat(solde_at_str)
        except ValueError:
            return _echec('Le champ Date doit être la date du jour.')
        if solde_at != date.today():
            return _echec('Le champ Date doit être la date du jour.')

        if not user_id or not user_id.isdigit():
            return _echec('Le champ Employé doit être défini.')

        compte_employe = User.objects.filter(pk=user_id, is_active=True).exclude(pk=request.user.pk).first()
        if compte_employe is None:
            return _echec('Le champ Employé doit être défini.')

        transactions_qs = self._transactions_non_soldees(compte_employe, solde_at)
        if not transactions_qs.exclude(libelle__libelle=LIBELLE_MISE_A_JOUR_SOLDE).exists():
            return _echec("Il n'y a pas de nouvelles transactions à solder pour l'employé.")

        try:
            correction_solde = Decimal(correction_solde_s) if correction_solde_s else Decimal('0')
        except InvalidOperation:
            return _echec('Le solde caisse doit être un nombre valide.')
        if correction_solde < 0:
            return _echec('Le solde caisse doit être supérieur ou égal à zéro.')

        calcul_solde = transactions_qs.aggregate(total=Sum('montant'))['total'] or Decimal('0')

        try:
            with transaction.atomic():
                hist = HistoriqueSoldeCompte.objects.create(
                    solde_at=solde_at,
                    calcul_solde=calcul_solde,
                    correction_solde=correction_solde,
                    observation=observation or None,
                    admin=request.user,
                    user=compte_employe,
                )

                # Capturées avant la création de la nouvelle transaction ci-dessous,
                # pour ne jamais l'y inclure elle-même (elle doit rester non soldée).
                a_solder_ids = list(transactions_qs.values_list('pk', flat=True))

                libelle_obj = LibelleTransaction.objects.get_or_create(libelle=LIBELLE_MISE_A_JOUR_SOLDE)[0]
                TransactionModel.objects.create(
                    user=compte_employe,
                    created_at=solde_at,
                    libelle=libelle_obj,
                    table_id=f'historiquesoldecompte_id_{hist.pk}',
                    montant=correction_solde,
                    hist_solde_compte=None,
                )

                TransactionModel.objects.filter(pk__in=a_solder_ids).update(hist_solde_compte=hist)
        except Exception:
            return _echec("Échec de l'enregistrement du solde : aucune donnée n'a été modifiée.")

        log_audit(
            AuditAction.CREATE, f'Mise à jour solde compte caisse {compte_employe} — {correction_solde} TND',
            table='HistoriqueSoldeCompte', record_id=hist.pk, new_value=model_to_dict(hist),
        )
        messages.success(request, f'Solde de {compte_employe} enregistré avec succès.')
        return redirect(f"{reverse('administration:solde-caisse')}?user_id={user_id}")

    def _post_ajouter_transfert(self, request):
        user_id                = request.POST.get('user_id', '').strip()
        transfere_at_str        = request.POST.get('transfere_at', '').strip()
        compte_source_id        = request.POST.get('compte_source_id', '').strip()
        libelle                 = request.POST.get('libelle', '').strip()
        compte_destination_id   = request.POST.get('compte_destination_id', '').strip()
        montant_s               = request.POST.get('montant', '').strip()

        def _echec(message):
            messages.error(request, message)
            context = self._build_context(
                request, user_id=user_id, page=1,
                transfere_overrides={
                    'transfere_at':          transfere_at_str,
                    'compte_source_id':      compte_source_id,
                    'libelle':               libelle,
                    'compte_destination_id': compte_destination_id,
                    'montant':               montant_s or '0.000',
                },
            )
            return render(request, self.template_name, context)

        if not user_id or not user_id.isdigit():
            return _echec('Aucun employé sélectionné.')

        try:
            transfere_at = date.fromisoformat(transfere_at_str)
        except ValueError:
            return _echec('La date du transfert doit être une date valide.')
        if transfere_at > date.today():
            return _echec("La date du transfert ne peut pas être postérieure à la date du jour.")

        if not compte_source_id or not compte_source_id.isdigit():
            return _echec('Le compte source doit être défini.')

        if not libelle:
            return _echec('Le libellé doit être défini.')

        try:
            montant = Decimal(montant_s) if montant_s else Decimal('0')
        except InvalidOperation:
            return _echec('Le montant doit être un nombre valide.')
        if montant < 0:
            return _echec('Le montant ne doit pas être négatif.')

        if not compte_destination_id or not compte_destination_id.isdigit():
            return _echec('Le compte destinataire doit être défini.')

        if compte_source_id != user_id and compte_destination_id != user_id:
            return _echec(
                "Le compte source ou le compte destinataire doit être l'employé sélectionné en Section 1.",
            )

        transfere = Transfere(
            transfere_at=transfere_at,
            compte_source_id=compte_source_id,
            libelle=libelle,
            compte_destination_id=compte_destination_id,
            montant=montant,
        )
        try:
            transfere.full_clean()
        except ValidationError as exc:
            return _echec(' '.join(m for msgs in exc.message_dict.values() for m in msgs))

        try:
            with transaction.atomic():
                transfere.save()
                transfere.creer_transactions()
        except Exception:
            return _echec("Échec de l'enregistrement du transfert : aucune donnée n'a été modifiée.")

        log_audit(
            AuditAction.CREATE,
            f'Création transfert {transfere.compte_source} → {transfere.compte_destination} '
            f'({transfere.montant} TND)',
            table='Transfere', record_id=transfere.pk, new_value=model_to_dict(transfere),
        )
        messages.success(request, f'Transfert de {transfere.montant} TND enregistré avec succès.')
        return redirect(f"{reverse('administration:solde-caisse')}?user_id={user_id}")
