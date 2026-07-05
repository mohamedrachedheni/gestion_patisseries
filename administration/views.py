import datetime
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, RestrictedError, Sum
from django.forms.models import model_to_dict
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import DetailView, ListView, TemplateView, View

from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin

from .forms import EmployeForm, TransfereForm, UserCreateForm, UserUpdateForm
from .models import Employe, LibelleTransaction, Transaction as TransactionModel, Transfere

User = get_user_model()


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = 'Administration'
    template_name = 'administration/home.html'


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
        date_debut_str = request.GET.get('date_debut', '').strip()
        date_fin_str   = request.GET.get('date_fin',   '').strip()
        user_id        = request.GET.get('user_id',    '').strip()
        libelle_id     = request.GET.get('libelle_id', '').strip()

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
            'users_list':     users_list,
            'libelles_list':  libelles_list,
            'totaux':         totaux,
            'total_count':    paginator.count,
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
