import datetime
import json
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO

from django.contrib import messages
from django.contrib.auth import get_user_model
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
    Achat, AchatDetaille, BonLivraison, BonLivraisonDetaille, Delegation, Gouvernorat, Zone,
)
from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin
from production.models import CommandeInterne, MatierePremiere, Produit, Recette, RecetteDetaille
from production.services import (
    calculer_besoins_matieres_premieres_periode,
    calculer_besoins_matieres_premieres_pour_semi_finis_periode,
    quantite_commandee_semi_finis_periode,
    recettes_produit_unite_map,
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
from .models import Employe, LibelleTransaction, Transaction as TransactionModel, Transfere

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
        instance = get_object_or_404(Gouvernorat, pk=pk)
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
    PAGINATE_BY = 5

    def get(self, request):
        qs = Delegation.objects.select_related('gouvernorat').order_by('gouvernorat__nom', 'nom_delegation')
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':       page_obj,
            'is_paginated':   page_obj.has_other_pages(),
            'delegations':    page_obj.object_list,
            'gouvernorats_list': Gouvernorat.objects.order_by('nom'),
            'add_form':       DelegationForm(),
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
        instance = get_object_or_404(Delegation, pk=pk)
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
    PAGINATE_BY = 5

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
        instance = get_object_or_404(Zone, pk=pk)
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
        qs = Employe.objects.select_related('user')
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
        return UserCreateForm(data), EmployeForm(data, files)

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
            'employe_form': EmployeForm(instance=employe),
            'employe': employe,
            'title': f'Modifier — {employe.user.get_full_name()}',
            'is_update': True,
        })

    def post(self, request, pk):
        employe = self._get(pk)
        avant = model_to_dict(employe)
        user_form = UserUpdateForm(request.POST, instance=employe.user)
        employe_form = EmployeForm(request.POST, request.FILES, instance=employe)
        if user_form.is_valid() and employe_form.is_valid():
            with transaction.atomic():
                user_form.save()
                employe_form.save()
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
        return get_object_or_404(Employe.objects.select_related('user'), pk=pk)

    def get(self, request, pk):
        return render(request, self.template_name, {'employe': self._get(pk)})

    def post(self, request, pk):
        employe = self._get(pk)
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
            .filter(transactions__isnull=False)
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


_BON_LIVRAISON_TABLE_ID_RE = re.compile(r'^BonLivraison_id_(\d+)$')


class TransactionDetailPopupView(GroupRequiredMixin, View):
    """Contenu (fragment HTML, chargé en AJAX dans la pop-up « Détail ») de la
    page Transactions. Pour l'instant, seules les transactions liées à un
    commercial_bonlivraison (table_id="BonLivraison_id_<id>") affichent
    quelque chose — les autres tables restent à suivre.

    Section 1 : BonLivraisonCode (bon_livraison_at, bon_livraison_numero,
    client), sur une seule ligne.
    Section 1-1 : sous-formulaire — les BonLivraison liés dynamiquement à ce
    même bon_livraison_code_id, paginés un enregistrement à la fois (page par
    défaut = celui référencé par la transaction elle-même).
    Section 1-1-1 : sous-formulaire de la 1-1 — les BonLivraisonDetaille du
    BonLivraison actuellement affiché, sans paginateur."""
    group_required = 'Administration'
    template_name = 'administration/transaction/detail_popup.html'

    def get(self, request, pk):
        transaction_obj = get_object_or_404(TransactionModel, pk=pk)
        match = _BON_LIVRAISON_TABLE_ID_RE.match(transaction_obj.table_id or '')

        if not match:
            return render(request, self.template_name, {
                'supported':   False,
                'transaction': transaction_obj,
            })

        bon_livraison_id = int(match.group(1))
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
            'supported':          True,
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
        return get_object_or_404(
            Transfere.objects.select_related('compte_source', 'compte_destination'), pk=pk,
        )

    def get(self, request, pk):
        transfere = self._get(pk)
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
            .filter(transferts_emis__isnull=False)
            .distinct()
            .order_by('last_name', 'first_name')
        )
        libelles_list = _libelles_transfere_distincts()
        # Comptes proposés dans les selects Opérateur/Receveur de chaque ligne
        users_list = User.objects.filter(is_active=True).order_by('last_name', 'first_name')

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

        # ── Nouvelle partie : commandes internes de la période (avant la 1) ─
        commandes_internes_periode = (
            CommandeInterne.objects
            .filter(livraison_at__gte=date_debut, livraison_at__lte=date_fin)
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
