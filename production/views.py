import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, RestrictedError
from django.forms.models import model_to_dict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import TemplateView, View

from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin
from core.models import JourNonOuvre

from .forms import FamilleForm, MatierePremiereForm, ProduitForm, ProduitSemiFiniForm
from .models import CommandeInterne, MatierePremiere, MatierePremiereFamille, Produit, Recette, RecetteDetaille
from .services import (
    MiseAJourStockError,
    appliquer_mise_a_jour_stock_commande_interne,
    recettes_produit_unite_map,
)


def _form_errors_text(form):
    return ' '.join(e for errors in form.errors.values() for e in errors) or 'Données invalides.'


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = ['Administration', 'Production']
    template_name = 'production/home.html'


# ─── Produits ───────────────────────────────────────────────────────────────

class ProduitListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/produit/list.html'
    PAGINATE_BY = 25

    def get(self, request):
        nom          = request.GET.get('nom', '').strip()
        stock_faible = request.GET.get('stock_faible', '').strip()
        prix_max_str = request.GET.get('prix_max', '').strip()

        qs = Produit.objects.filter(is_produit_semi_fini=False)

        if nom:
            qs = qs.filter(nom__icontains=nom)
        if stock_faible:
            qs = qs.filter(stock__lt=F('quantite_minimale'))
        if prix_max_str:
            try:
                qs = qs.filter(prix_unitaire__lt=float(prix_max_str))
            except ValueError:
                prix_max_str = ''

        qs = qs.order_by('nom')

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':     page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'nom':          nom,
            'stock_faible': stock_faible,
            'prix_max':     prix_max_str,
            'total_count':  paginator.count,
        })


class ProduitDetailView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/produit/detail.html'

    def get(self, request, pk):
        produit = get_object_or_404(Produit, pk=pk)
        return render(request, self.template_name, {'produit': produit})


class ProduitCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/produit/form.html'

    def get(self, request):
        return render(request, self.template_name, {
            'form': ProduitForm(),
            'title': 'Nouveau produit',
        })

    def post(self, request):
        form = ProduitForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form,
                'title': 'Nouveau produit',
            })

        produit = form.save(commit=False)
        produit.is_produit_semi_fini = False
        produit.save()

        log_audit(
            AuditAction.CREATE, f'Création produit « {produit.nom} »',
            table='Produit', record_id=produit.pk, new_value=model_to_dict(produit),
        )
        messages.success(request, f'Produit « {produit.nom} » créé avec succès.')
        return redirect('production:produit-detail', pk=produit.pk)


class ProduitUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/produit/form.html'

    def _get(self, pk):
        return get_object_or_404(Produit, pk=pk)

    def get(self, request, pk):
        produit = self._get(pk)
        return render(request, self.template_name, {
            'form': ProduitForm(instance=produit),
            'title': f'Modifier — {produit.nom}',
            'produit': produit,
            'is_update': True,
        })

    def post(self, request, pk):
        produit = self._get(pk)
        avant = model_to_dict(produit)
        form = ProduitForm(request.POST, instance=produit)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form,
                'title': f'Modifier — {produit.nom}',
                'produit': produit,
                'is_update': True,
            })

        produit = form.save(commit=False)
        produit.is_produit_semi_fini = False
        produit.save()

        log_audit(
            AuditAction.UPDATE, f'Modification produit « {produit.nom} »',
            table='Produit', record_id=produit.pk, old_value=avant, new_value=model_to_dict(produit),
        )
        messages.success(request, f'Produit « {produit.nom} » modifié avec succès.')
        return redirect('production:produit-detail', pk=produit.pk)


class ProduitDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/produit/confirm_delete.html'

    def _get(self, pk):
        return Produit.objects.filter(pk=pk).first()

    def get(self, request, pk):
        produit = self._get(pk)
        if produit is None:
            messages.info(request, 'Ce produit a déjà été supprimé.')
            return redirect('production:produit-list')
        return render(request, self.template_name, {'produit': produit})

    def post(self, request, pk):
        produit = self._get(pk)
        if produit is None:
            messages.info(request, 'Ce produit a déjà été supprimé.')
            return redirect('production:produit-list')
        nom = produit.nom
        avant = model_to_dict(produit)
        try:
            produit.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : il est encore lié à d'autres "
                "enregistrements (recettes, commandes, bons de sortie/restitution, "
                "livraisons…).",
            )
            return redirect('production:produit-detail', pk=pk)

        log_audit(
            AuditAction.DELETE, f'Suppression produit « {nom} »',
            table='Produit', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Produit « {nom} » supprimé avec succès.')
        return redirect('production:produit-list')


# ─── Familles de matière première ────────────────────────────────────────────

class FamilleListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/famille/list.html'
    PAGINATE_BY = 4

    def get(self, request):
        qs = MatierePremiereFamille.objects.order_by('famille')
        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':     page_obj,
            'is_paginated': page_obj.has_other_pages(),
            'familles':     page_obj.object_list,
            'add_form':     FamilleForm(),
        })


class FamilleCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request):
        form = FamilleForm(request.POST)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('production:famille-list')

        famille = form.save()
        log_audit(
            AuditAction.CREATE, f'Création famille de matière première « {famille.famille} »',
            table='MatierePremiereFamille', record_id=famille.pk, new_value=model_to_dict(famille),
        )
        messages.success(request, f'Famille « {famille.famille} » ajoutée avec succès.')
        return redirect('production:famille-list')


class FamilleUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        instance = get_object_or_404(MatierePremiereFamille, pk=pk)
        avant = model_to_dict(instance)
        form = FamilleForm(request.POST, instance=instance)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect('production:famille-list')

        famille = form.save()
        log_audit(
            AuditAction.UPDATE, f'Modification famille de matière première « {famille.famille} »',
            table='MatierePremiereFamille', record_id=famille.pk, old_value=avant, new_value=model_to_dict(famille),
        )
        messages.success(request, f'Famille « {famille.famille} » modifiée avec succès.')
        return redirect('production:famille-list')


class FamilleDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        instance = MatierePremiereFamille.objects.filter(pk=pk).first()
        if instance is None:
            messages.info(request, 'Cette famille de matière première a déjà été supprimée.')
            return redirect('production:famille-list')
        nom = instance.famille
        avant = model_to_dict(instance)
        try:
            instance.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : des matières premières sont "
                "encore rattachées à cette famille.",
            )
        else:
            log_audit(
                AuditAction.DELETE, f'Suppression famille de matière première « {nom} »',
                table='MatierePremiereFamille', record_id=pk, old_value=avant,
            )
            messages.success(request, f'Famille « {nom} » supprimée avec succès.')
        return redirect('production:famille-list')


# ─── Matières premières ───────────────────────────────────────────────────────

class MatierePremiereListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/matiere_premiere/list.html'
    PAGINATE_BY = 25

    def get(self, request):
        nom             = request.GET.get('nom', '').strip()
        quantite_faible = request.GET.get('quantite_faible', '').strip()
        famille_id      = request.GET.get('famille_id', '').strip()

        qs = MatierePremiere.objects.filter(produit__isnull=True).select_related('matiere_premiere_famille')

        if nom:
            qs = qs.filter(nom__icontains=nom)
        if quantite_faible:
            qs = qs.filter(quantite__lt=F('stock_minimum'))
        if famille_id:
            qs = qs.filter(matiere_premiere_famille_id=famille_id)

        qs = qs.order_by('nom')

        paginator = Paginator(qs, self.PAGINATE_BY)
        page_obj  = paginator.get_page(request.GET.get('page', 1))

        return render(request, self.template_name, {
            'page_obj':        page_obj,
            'is_paginated':    page_obj.has_other_pages(),
            'nom':             nom,
            'quantite_faible': quantite_faible,
            'famille_id':      famille_id,
            'familles_list':   MatierePremiereFamille.objects.order_by('famille'),
            'total_count':     paginator.count,
        })


class MatierePremiereDetailView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/matiere_premiere/detail.html'

    def get(self, request, pk):
        mp = get_object_or_404(MatierePremiere.objects.select_related('matiere_premiere_famille'), pk=pk)
        return render(request, self.template_name, {'matiere_premiere': mp})


class MatierePremiereCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/matiere_premiere/form.html'

    def get(self, request):
        return render(request, self.template_name, {
            'form': MatierePremiereForm(),
            'title': 'Nouvelle matière première',
        })

    def post(self, request):
        form = MatierePremiereForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form,
                'title': 'Nouvelle matière première',
            })

        mp = form.save(commit=False)
        mp.produit = None
        mp.save()

        log_audit(
            AuditAction.CREATE, f'Création matière première « {mp.nom} »',
            table='MatierePremiere', record_id=mp.pk, new_value=model_to_dict(mp),
        )
        messages.success(request, f'Matière première « {mp.nom} » créée avec succès.')
        return redirect('production:matiere-premiere-detail', pk=mp.pk)


class MatierePremiereUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/matiere_premiere/form.html'

    def _get(self, pk):
        return get_object_or_404(MatierePremiere, pk=pk)

    def get(self, request, pk):
        mp = self._get(pk)
        return render(request, self.template_name, {
            'form': MatierePremiereForm(instance=mp),
            'title': f'Modifier — {mp.nom}',
            'matiere_premiere': mp,
            'is_update': True,
        })

    def post(self, request, pk):
        mp = self._get(pk)
        avant = model_to_dict(mp)
        form = MatierePremiereForm(request.POST, instance=mp)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form,
                'title': f'Modifier — {mp.nom}',
                'matiere_premiere': mp,
                'is_update': True,
            })

        mp = form.save(commit=False)
        mp.produit = None
        mp.save()

        log_audit(
            AuditAction.UPDATE, f'Modification matière première « {mp.nom} »',
            table='MatierePremiere', record_id=mp.pk, old_value=avant, new_value=model_to_dict(mp),
        )
        messages.success(request, f'Matière première « {mp.nom} » modifiée avec succès.')
        return redirect('production:matiere-premiere-detail', pk=mp.pk)


class MatierePremiereDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/matiere_premiere/confirm_delete.html'

    def _get(self, pk):
        return MatierePremiere.objects.filter(pk=pk).first()

    def get(self, request, pk):
        mp = self._get(pk)
        if mp is None:
            messages.info(request, 'Cette matière première a déjà été supprimée.')
            return redirect('production:matiere-premiere-list')
        return render(request, self.template_name, {'matiere_premiere': mp})

    def post(self, request, pk):
        mp = self._get(pk)
        if mp is None:
            messages.info(request, 'Cette matière première a déjà été supprimée.')
            return redirect('production:matiere-premiere-list')
        nom = mp.nom
        avant = model_to_dict(mp)
        try:
            mp.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : elle est encore liée à d'autres "
                "enregistrements (recettes, achats…).",
            )
            return redirect('production:matiere-premiere-detail', pk=pk)

        log_audit(
            AuditAction.DELETE, f'Suppression matière première « {nom} »',
            table='MatierePremiere', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Matière première « {nom} » supprimée avec succès.')
        return redirect('production:matiere-premiere-list')


# ─── Produits semi-finis (Produit + MatierePremiere, relation circulaire) ────
#
# Un produit semi-fini est représenté par DEUX enregistrements liés :
#   - Produit (is_produit_semi_fini=True)
#   - MatierePremiere (produit=<le Produit ci-dessus>)
# Les deux `nom` sont tenus synchronisés ; création/modification/suppression
# se font toujours sur la paire, jamais sur un seul des deux enregistrements.

MSG_DOUBLON_PRODUIT = (
    "Le nom de ce produit semi-fini existe déjà dans la table Produit. "
    "Veuillez choisir un autre nom ou modifier l'enregistrement existant."
)
MSG_DOUBLON_MATIERE_PREMIERE = (
    "Le nom de ce produit semi-fini existe déjà dans la table matière première. "
    "Veuillez choisir un autre nom ou modifier l'enregistrement existant."
)


def _nom_semi_fini_conflit(nom, exclure_produit_pk=None, exclure_mp_pk=None):
    """None si `nom` est disponible, sinon le message d'erreur adapté à la
    table où le doublon a été trouvé (Produit vérifié en premier)."""
    produits = Produit.objects.filter(nom=nom)
    if exclure_produit_pk:
        produits = produits.exclude(pk=exclure_produit_pk)
    if produits.exists():
        return MSG_DOUBLON_PRODUIT

    matieres = MatierePremiere.objects.filter(nom=nom)
    if exclure_mp_pk:
        matieres = matieres.exclude(pk=exclure_mp_pk)
    if matieres.exists():
        return MSG_DOUBLON_MATIERE_PREMIERE

    return None


def _produit_semi_fini_context(request):
    nom             = request.GET.get('nom', '').strip()
    famille_id      = request.GET.get('famille_id', '').strip()
    quantite_faible = request.GET.get('quantite_faible', '').strip()

    qs = (
        MatierePremiere.objects
        .filter(produit__isnull=False)
        .select_related('matiere_premiere_famille', 'produit')
    )
    if nom:
        qs = qs.filter(nom__icontains=nom)
    if famille_id:
        qs = qs.filter(matiere_premiere_famille_id=famille_id)
    if quantite_faible:
        qs = qs.filter(quantite__lt=F('stock_minimum'))
    qs = qs.order_by('nom')

    paginator = Paginator(qs, 4)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    return {
        'page_obj':        page_obj,
        'is_paginated':    page_obj.has_other_pages(),
        'nom':             nom,
        'famille_id':      famille_id,
        'quantite_faible': quantite_faible,
        'familles_list':   MatierePremiereFamille.objects.order_by('famille'),
        'unite_choices':   MatierePremiere.UNITE_CHOICES,
        'total_count':     paginator.count,
        'focus_nom':       request.GET.get('focus') == 'nom',
    }


class ProduitSemiFiniListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/produit_semi_fini/list.html'

    def get(self, request):
        return render(request, self.template_name, _produit_semi_fini_context(request))


class ProduitSemiFiniCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request):
        list_url = reverse('production:produit-semi-fini-list')

        form = ProduitSemiFiniForm(request.POST)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect(f'{list_url}?focus=nom')

        nom = form.cleaned_data['nom']
        conflit = _nom_semi_fini_conflit(nom)
        if conflit:
            messages.error(request, conflit)
            return redirect(f'{list_url}?focus=nom')

        with transaction.atomic():
            produit = Produit.objects.create(nom=nom, is_produit_semi_fini=True)
            mp = MatierePremiere.objects.create(
                nom=nom,
                matiere_premiere_famille=form.cleaned_data['matiere_premiere_famille'],
                unite=form.cleaned_data['unite'] or None,
                quantite=form.cleaned_data['quantite'],
                stock_minimum=form.cleaned_data['stock_minimum'],
                produit=produit,
            )

        log_audit(
            AuditAction.CREATE, f'Création produit semi-fini « {nom} »',
            table='ProduitSemiFini', record_id=produit.pk,
            new_value={'produit': model_to_dict(produit), 'matiere_premiere': model_to_dict(mp)},
        )
        messages.success(request, f'Produit semi-fini « {nom} » créé avec succès.')
        return redirect('production:produit-semi-fini-list')


class ProduitSemiFiniUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        list_url = reverse('production:produit-semi-fini-list')
        mp = get_object_or_404(MatierePremiere.objects.select_related('produit'), pk=pk)
        produit = mp.produit
        if produit is None:
            messages.error(request, "Enregistrement incohérent : aucun produit lié dans la table Produit.")
            return redirect(list_url)

        form = ProduitSemiFiniForm(request.POST)
        if not form.is_valid():
            messages.error(request, _form_errors_text(form))
            return redirect(list_url)

        nom = form.cleaned_data['nom']
        if nom != mp.nom:
            conflit = _nom_semi_fini_conflit(nom, exclure_produit_pk=produit.pk, exclure_mp_pk=mp.pk)
            if conflit:
                messages.error(request, conflit)
                return redirect(f'{list_url}?focus=nom')

        avant = {'produit': model_to_dict(produit), 'matiere_premiere': model_to_dict(mp)}

        with transaction.atomic():
            mp.nom = nom
            mp.matiere_premiere_famille = form.cleaned_data['matiere_premiere_famille']
            mp.unite = form.cleaned_data['unite'] or None
            mp.quantite = form.cleaned_data['quantite']
            mp.stock_minimum = form.cleaned_data['stock_minimum']
            mp.save()
            produit.nom = nom
            produit.save(update_fields=['nom'])

        log_audit(
            AuditAction.UPDATE, f'Modification produit semi-fini « {nom} »',
            table='ProduitSemiFini', record_id=produit.pk, old_value=avant,
            new_value={'produit': model_to_dict(produit), 'matiere_premiere': model_to_dict(mp)},
        )
        messages.success(request, f'Produit semi-fini « {nom} » modifié avec succès.')
        return redirect('production:produit-semi-fini-list')


class ProduitSemiFiniDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        mp = MatierePremiere.objects.select_related('produit').filter(pk=pk).first()
        if mp is None:
            messages.info(request, 'Ce produit semi-fini a déjà été supprimé.')
            return redirect('production:produit-semi-fini-list')
        produit = mp.produit
        nom = mp.nom
        avant = {
            'produit': model_to_dict(produit) if produit else None,
            'matiere_premiere': model_to_dict(mp),
        }
        try:
            with transaction.atomic():
                mp.delete()
                if produit is not None:
                    produit.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer « {nom} » : il est encore lié à d'autres "
                "enregistrements (recettes, achats, commandes, bons de sortie/restitution, "
                "livraisons…).",
            )
            return redirect('production:produit-semi-fini-list')

        log_audit(
            AuditAction.DELETE, f'Suppression produit semi-fini « {nom} »',
            table='ProduitSemiFini', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Produit semi-fini « {nom} » supprimé avec succès.')
        return redirect('production:produit-semi-fini-list')


# ─── Recettes ─────────────────────────────────────────────────────────────────

PAGINATE_RECETTES = 8


def _recette_list_context(request):
    nom = request.GET.get('nom', '').strip()

    qs = Recette.objects.select_related('produit').order_by('produit__nom')
    if nom:
        qs = qs.filter(produit__nom__icontains=nom)

    paginator = Paginator(qs, PAGINATE_RECETTES)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    return {
        'page_obj':     page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'nom':          nom,
        'total_count':  paginator.count,
    }


class RecetteListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/recette/list.html'

    def get(self, request):
        return render(request, self.template_name, _recette_list_context(request))


def _recette_detail_context(recette):
    return {
        'title': f'Détail de la recette — {recette.produit.nom}',
        'recette': recette,
        'details': (
            recette.details
            .select_related('matiere_premiere')
            .order_by('matiere_premiere__nom')
        ),
    }


class RecetteDetailView(GroupRequiredMixin, View):
    """Détail en lecture seule d'une recette — bouton « Visualiser » de la
    liste. Tous les champs sont verrouillés ; pas d'ajout/suppression de
    ligne ni d'enregistrement (voir RecetteUpdateView pour la modification)."""
    group_required = ['Administration', 'Production']
    template_name = 'production/recette/detail.html'

    def get(self, request, pk):
        recette = get_object_or_404(Recette.objects.select_related('produit'), pk=pk)
        return render(request, self.template_name, _recette_detail_context(recette))


class RecetteDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        recette = Recette.objects.select_related('produit').filter(pk=pk).first()
        if recette is None:
            messages.info(request, 'Cette recette a déjà été supprimée.')
            return redirect('production:recette-list')
        nom_produit = recette.produit.nom
        nb_details = recette.details.count()
        avant = {'recette': model_to_dict(recette), 'nb_details': nb_details}
        try:
            recette.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer la recette « {nom_produit} » : elle est encore liée "
                "à d'autres enregistrements.",
            )
            return redirect('production:recette-list')

        log_audit(
            AuditAction.DELETE, f'Suppression recette « {nom_produit} » ({nb_details} ligne(s))',
            table='Recette', record_id=pk, old_value=avant,
        )
        messages.success(request, f'Recette « {nom_produit} » supprimée avec succès.')
        return redirect('production:recette-list')


def _recette_create_context(request, **overrides):
    matieres = MatierePremiere.objects.order_by('nom')
    context = {
        'title': 'Nouvelle recette',
        'produits_disponibles': Produit.objects.filter(recettes__isnull=True).order_by('nom'),
        'unite_choices': Recette.UNITE_CHOICES,
        'selected_produit_id': '',
        'quantite': '0',
        'unite': 'Pièce',
        'observation': '',
        'detail_rows_json': '[]',
        # produit_id : sert au JS à exclure, pour un produit semi-fini, la
        # matière première qui lui est associée (une recette ne peut pas
        # avoir pour ingrédient la matière première qui la représente elle-même).
        'matieres_json': json.dumps([
            {'id': mp.pk, 'nom': mp.nom, 'unite': mp.unite or '', 'produit_id': mp.produit_id}
            for mp in matieres
        ]),
        'focus_produit': False,
        'focus_quantite': False,
    }
    context.update(overrides)
    return context


class RecetteCreateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/recette/create.html'

    def get(self, request):
        return render(request, self.template_name, _recette_create_context(request))

    def post(self, request):
        produit_id   = request.POST.get('produit_id', '').strip()
        quantite_str = request.POST.get('quantite', '').strip()
        unite        = request.POST.get('unite', '').strip() or 'Pièce'
        observation  = request.POST.get('observation', '').strip()

        mp_ids    = request.POST.getlist('matiere_premiere_id')
        qte_strs  = request.POST.getlist('quantite_detail')
        raw_rows  = [{'matiere_premiere_id': m, 'quantite': q} for m, q in zip(mp_ids, qte_strs)]

        def _echec(message, *, focus_produit=False, focus_quantite=False, reset_details=False):
            messages.error(request, message)
            context = _recette_create_context(
                request,
                selected_produit_id=produit_id,
                quantite=quantite_str or '0',
                unite=unite,
                observation=observation,
                detail_rows_json='[]' if reset_details else json.dumps(raw_rows),
                focus_produit=focus_produit,
                focus_quantite=focus_quantite,
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : Recette (partie 1 du template) ──────────────────────
        if not produit_id:
            return _echec("Veuillez sélectionner un produit.", focus_produit=True)

        produit = Produit.objects.filter(pk=produit_id).first()
        if produit is None:
            return _echec("Produit introuvable.", focus_produit=True)

        if Recette.objects.filter(produit_id=produit_id).exists():
            return _echec(
                f"Le produit « {produit.nom} » a déjà une recette. Veuillez modifier la "
                "recette existante ou choisir un autre produit.",
                focus_produit=True,
            )

        try:
            quantite = Decimal(quantite_str) if quantite_str else Decimal('0')
        except InvalidOperation:
            return _echec("Quantité invalide.", focus_quantite=True)

        if quantite <= 0:
            return _echec("La quantité doit être strictement supérieure à zéro.", focus_quantite=True)

        # ── Étape 2 : RecetteDetaille (partie 2 du template) ──────────────
        # Lignes ignorées silencieusement : matière première ou quantité absente/≤ 0.
        lignes_valides = []
        for mp_id, qte_str in zip(mp_ids, qte_strs):
            mp_id = (mp_id or '').strip()
            qte_str = (qte_str or '').strip()
            if not mp_id:
                continue
            try:
                qte = Decimal(qte_str) if qte_str else Decimal('0')
            except InvalidOperation:
                continue
            if qte <= 0:
                continue
            lignes_valides.append((mp_id, qte))

        if not lignes_valides:
            return _echec(
                "Veuillez renseigner au moins une ligne de matière première valide "
                "(nom et quantité strictement positive).",
                reset_details=True,
            )

        # Un produit semi-fini ne peut pas figurer comme ingrédient de sa propre
        # recette (relation circulaire Produit ↔ MatierePremiere sur lui-même).
        matiere_propre = MatierePremiere.objects.filter(produit_id=produit_id).first()
        if matiere_propre and str(matiere_propre.pk) in {mp_id for mp_id, _ in lignes_valides}:
            return _echec(
                f"Le produit « {produit.nom} » est lui-même enregistré comme matière première "
                f"(« {matiere_propre.nom} ») : il ne peut pas figurer comme ingrédient de sa "
                "propre recette. Veuillez retirer ou remplacer cette ligne.",
            )

        comptes = {}
        for mp_id, _ in lignes_valides:
            comptes[mp_id] = comptes.get(mp_id, 0) + 1
        doublons = [mp_id for mp_id, n in comptes.items() if n > 1]
        if doublons:
            noms = list(MatierePremiere.objects.filter(pk__in=doublons).values_list('nom', flat=True))
            return _echec(
                "Une même matière première ne peut apparaître qu'une seule fois dans la "
                f"recette. Doublon(s) détecté(s) : {', '.join(noms)}. Veuillez corriger avant "
                "d'enregistrer.",
            )

        matieres_par_id = {
            str(mp.pk): mp
            for mp in MatierePremiere.objects.filter(pk__in=[mp_id for mp_id, _ in lignes_valides])
        }
        if len(matieres_par_id) != len(lignes_valides):
            # Une matière première sélectionnée a été supprimée entre l'affichage et l'envoi.
            return _echec(
                "Une ou plusieurs matières premières sélectionnées n'existent plus. "
                "Veuillez rafraîchir la page et recommencer.",
            )

        # ── Enregistrement atomique : rien n'est écrit si une étape échoue ─
        with transaction.atomic():
            recette = Recette.objects.create(
                produit=produit, quantite=quantite, unite=unite, observation=observation or None,
            )
            RecetteDetaille.objects.bulk_create([
                RecetteDetaille(recette=recette, matiere_premiere=matieres_par_id[mp_id], quantite=qte)
                for mp_id, qte in lignes_valides
            ])

        log_audit(
            AuditAction.CREATE, f'Création recette « {produit.nom} » ({len(lignes_valides)} ligne(s))',
            table='Recette', record_id=recette.pk,
            new_value={
                'recette': model_to_dict(recette),
                'details': [
                    {'matiere_premiere': matieres_par_id[mp_id].nom, 'quantite': str(qte)}
                    for mp_id, qte in lignes_valides
                ],
            },
        )
        messages.success(
            request,
            f'Recette « {produit.nom} » créée avec succès ({len(lignes_valides)} ligne(s)).',
        )
        return redirect('production:recette-detail', pk=recette.pk)


def _recette_detail_rows_json(recette):
    return json.dumps([
        {'matiere_premiere_id': d.matiere_premiere_id, 'quantite': str(d.quantite)}
        for d in recette.details.select_related('matiere_premiere').order_by('matiere_premiere__nom')
    ])


def _recette_update_context(request, recette, **overrides):
    # Le produit ne change jamais ici : on exclut directement du queryset la
    # matière première qui le représente lui-même (produit semi-fini), pas
    # besoin du filtrage JS dynamique utilisé côté création (pas de <select>
    # produit à surveiller).
    matieres = MatierePremiere.objects.exclude(produit_id=recette.produit_id).order_by('nom')
    context = {
        'title': f'Modifier la recette — {recette.produit.nom}',
        'recette': recette,
        'unite_choices': Recette.UNITE_CHOICES,
        'quantite': str(recette.quantite),
        'unite': recette.unite or 'Pièce',
        'observation': recette.observation or '',
        'detail_rows_json': _recette_detail_rows_json(recette),
        'matieres_json': json.dumps([
            {'id': mp.pk, 'nom': mp.nom, 'unite': mp.unite or ''} for mp in matieres
        ]),
        'focus_quantite': False,
    }
    context.update(overrides)
    return context


class RecetteUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/recette/update.html'

    def _get(self, pk):
        return get_object_or_404(Recette.objects.select_related('produit'), pk=pk)

    def get(self, request, pk):
        recette = self._get(pk)
        return render(request, self.template_name, _recette_update_context(request, recette))

    def post(self, request, pk):
        recette = self._get(pk)

        quantite_str = request.POST.get('quantite', '').strip()
        unite        = request.POST.get('unite', '').strip() or 'Pièce'
        observation  = request.POST.get('observation', '').strip()

        mp_ids   = request.POST.getlist('matiere_premiere_id')
        qte_strs = request.POST.getlist('quantite_detail')
        raw_rows = [{'matiere_premiere_id': m, 'quantite': q} for m, q in zip(mp_ids, qte_strs)]
        anciennes_lignes_json = _recette_detail_rows_json(recette)

        def _echec(message, *, focus_quantite=False, reset_details=False):
            messages.error(request, message)
            context = _recette_update_context(
                request, recette,
                quantite=quantite_str or '0',
                unite=unite,
                observation=observation,
                detail_rows_json=anciennes_lignes_json if reset_details else json.dumps(raw_rows),
                focus_quantite=focus_quantite,
            )
            return render(request, self.template_name, context)

        # ── Étape 1 : Recette (partie 1 du template) — produit_id inchangé ─
        try:
            quantite = Decimal(quantite_str) if quantite_str else Decimal('0')
        except InvalidOperation:
            return _echec("Quantité invalide.", focus_quantite=True)

        if quantite <= 0:
            return _echec("La quantité doit être strictement supérieure à zéro.", focus_quantite=True)

        # ── Étape 2 : RecetteDetaille (partie 2 du template) ──────────────
        # Toutes les vérifications (étape 1 et étape 2) se font avant de
        # toucher aux anciens enregistrements de Recette et RecetteDetaille.
        lignes_valides = []
        for mp_id, qte_str in zip(mp_ids, qte_strs):
            mp_id = (mp_id or '').strip()
            qte_str = (qte_str or '').strip()
            if not mp_id:
                continue
            try:
                qte = Decimal(qte_str) if qte_str else Decimal('0')
            except InvalidOperation:
                continue
            if qte <= 0:
                continue
            lignes_valides.append((mp_id, qte))

        if not lignes_valides:
            return _echec(
                "Veuillez renseigner au moins une ligne de matière première valide "
                "(nom et quantité strictement positive).",
                reset_details=True,
            )

        matiere_propre = MatierePremiere.objects.filter(produit_id=recette.produit_id).first()
        if matiere_propre and str(matiere_propre.pk) in {mp_id for mp_id, _ in lignes_valides}:
            return _echec(
                f"Le produit « {recette.produit.nom} » est lui-même enregistré comme matière "
                f"première (« {matiere_propre.nom} ») : il ne peut pas figurer comme ingrédient "
                "de sa propre recette. Veuillez retirer ou remplacer cette ligne.",
            )

        comptes = {}
        for mp_id, _ in lignes_valides:
            comptes[mp_id] = comptes.get(mp_id, 0) + 1
        doublons = [mp_id for mp_id, n in comptes.items() if n > 1]
        if doublons:
            noms = list(MatierePremiere.objects.filter(pk__in=doublons).values_list('nom', flat=True))
            return _echec(
                "Une même matière première ne peut apparaître qu'une seule fois dans la "
                f"recette. Doublon(s) détecté(s) : {', '.join(noms)}. Veuillez corriger avant "
                "d'enregistrer.",
            )

        matieres_par_id = {
            str(mp.pk): mp
            for mp in MatierePremiere.objects.filter(pk__in=[mp_id for mp_id, _ in lignes_valides])
        }
        if len(matieres_par_id) != len(lignes_valides):
            return _echec(
                "Une ou plusieurs matières premières sélectionnées n'existent plus. "
                "Veuillez rafraîchir la page et recommencer.",
            )

        # ── Toutes les validations sont passées : on modifie maintenant ───
        # Anciennes valeurs capturées avant toute écriture. Si une étape
        # suivante échoue pour une raison imprévue, transaction.atomic()
        # restaure exactement cet état — le moteur de base de données joue
        # le rôle de la « table temporaire » : rien n'est perdu sans qu'il
        # soit nécessaire de dupliquer manuellement les anciennes lignes.
        avant = {
            'recette': model_to_dict(recette),
            'details': [
                {'matiere_premiere': d.matiere_premiere.nom, 'quantite': str(d.quantite)}
                for d in recette.details.select_related('matiere_premiere')
            ],
        }

        with transaction.atomic():
            recette.quantite = quantite
            recette.unite = unite
            recette.observation = observation or None
            recette.save()

            recette.details.all().delete()
            RecetteDetaille.objects.bulk_create([
                RecetteDetaille(recette=recette, matiere_premiere=matieres_par_id[mp_id], quantite=qte)
                for mp_id, qte in lignes_valides
            ])

        log_audit(
            AuditAction.UPDATE, f'Modification recette « {recette.produit.nom} » ({len(lignes_valides)} ligne(s))',
            table='Recette', record_id=recette.pk, old_value=avant,
            new_value={
                'recette': model_to_dict(recette),
                'details': [
                    {'matiere_premiere': matieres_par_id[mp_id].nom, 'quantite': str(qte)}
                    for mp_id, qte in lignes_valides
                ],
            },
        )
        messages.success(
            request,
            f'Recette « {recette.produit.nom} » modifiée avec succès ({len(lignes_valides)} ligne(s)).',
        )
        return redirect('production:recette-detail', pk=recette.pk)


# ─── Commandes internes ───────────────────────────────────────────────────────
#
# CommandeInterne référence une Recette (pas directement un Produit) : dans ce
# template, le champ « Produit » que l'utilisateur manipule est un raccourci
# pour choisir la Recette de ce produit (une seule recette par produit).

PAGINATE_COMMANDES = 4


def _resoudre_recette(produit_id):
    if not produit_id:
        return None
    return Recette.objects.select_related('produit').filter(produit_id=produit_id).first()


def _recettes_json():
    """Table produit_id → {recette_id, unite}, pour le JS (sélection d'un
    Produit ⇒ auto-remplissage de l'Unité, en lecture seule)."""
    return json.dumps(recettes_produit_unite_map())


def _commande_interne_list_context(request):
    today = date.today()
    date_debut_str    = request.GET.get('date_debut', '').strip()
    date_fin_str      = request.GET.get('date_fin', '').strip()
    produit_id        = request.GET.get('produit_id', '').strip()
    # Case décochée par défaut (y compris à l'activation du template, sans
    # querystring) : la valeur absente équivaut à '0', jamais à « pas de filtre ».
    is_completed_f    = request.GET.get('is_completed', '0').strip() == '1'
    is_stock_maj_f    = request.GET.get('is_stock_maj', '0').strip() == '1'
    # Bouton « Tout afficher » : ignore produit/Achevé/Stock MAJ, ne conserve
    # que la période.
    show_all          = request.GET.get('show_all', '').strip() == '1'
    if show_all:
        produit_id     = ''
        is_completed_f = False
        is_stock_maj_f = False

    # Par défaut : les sept prochains jours à partir d'aujourd'hui (échéances à venir).
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

    qs = (
        CommandeInterne.objects
        .select_related('recette__produit')
        .filter(livraison_at__gte=date_debut, livraison_at__lte=date_fin)
    )
    if produit_id:
        qs = qs.filter(recette__produit_id=produit_id)
    if not show_all:
        qs = qs.filter(is_completed=is_completed_f, is_stock_maj=is_stock_maj_f)
    qs = qs.order_by('livraison_at')

    paginator = Paginator(qs, PAGINATE_COMMANDES)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    produits_avec_recette = Produit.objects.filter(recettes__isnull=False).order_by('nom')

    # Jours non ouvrés (fermetures, fériés impactant la livraison) : chargés
    # sans filtre de date (table de référence, faible volume) pour couvrir à
    # la fois les lignes déjà enregistrées (Partie 1) et toute échéance future
    # saisie en Partie 2, indépendamment de la période filtrée ici.
    jours_non_ouvres = list(
        JourNonOuvre.objects.filter(concerne_livraison=True).values_list('date', flat=True)
    )

    return {
        'page_obj':               page_obj,
        'is_paginated':           page_obj.has_other_pages(),
        'date_debut':             date_debut_str,
        'date_fin':               date_fin_str,
        'produit_id':             produit_id,
        'is_completed_filter':    is_completed_f,
        'is_stock_maj_filter':    is_stock_maj_f,
        'produits_list':          produits_avec_recette,
        'produits_json':          json.dumps([{'id': p.pk, 'nom': p.nom} for p in produits_avec_recette]),
        'recettes_json':          _recettes_json(),
        'unite_choices':          Recette.UNITE_CHOICES,
        'total_count':            paginator.count,
        'querystring':            request.GET.urlencode(),
        'jours_non_ouvres_dates': set(jours_non_ouvres),
        'jours_non_ouvres_json':  json.dumps([d.isoformat() for d in jours_non_ouvres]),
    }


def _commande_interne_part2_context(**overrides):
    context = {
        'part2_rows_json':  '[]',
    }
    context.update(overrides)
    return context


def _safe_next_url(request):
    """URL de retour explicite (champ « next », POST ou GET) — permet de
    réutiliser les vues CommandeInterne depuis une autre page (ex : le
    contrôle de rupture de stock) en y revenant après l'action, au lieu du
    retour par défaut vers production/commandes-internes/. Valide l'hôte pour
    éviter une redirection ouverte."""
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return next_url
    return None


def _commande_interne_redirect(request):
    next_url = _safe_next_url(request)
    if next_url:
        return redirect(next_url)
    qs = request.META.get('QUERY_STRING', '')
    url = reverse('production:commande-interne-list')
    return redirect(f'{url}?{qs}' if qs else url)


class CommandeInterneListView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']
    template_name = 'production/commande_interne/list.html'

    def get(self, request):
        context = _commande_interne_list_context(request)
        context.update(_commande_interne_part2_context())
        return render(request, self.template_name, context)


class CommandeInterneUpdateView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        ci = get_object_or_404(CommandeInterne.objects.select_related('recette__produit'), pk=pk)

        if ci.is_stock_maj:
            messages.error(
                request,
                "Cette commande est verrouillée : le stock a déjà été mis à jour.",
            )
            return _commande_interne_redirect(request)

        livraison_at_str        = request.POST.get('livraison_at', '').strip()
        produit_id               = request.POST.get('produit_id', '').strip()
        is_completed             = 'is_completed' in request.POST
        quantite_commander_str   = request.POST.get('quantite_commander')
        quantite_produite_str    = request.POST.get('quantite_produite')

        if not livraison_at_str:
            messages.error(request, "Veuillez renseigner l'échéance.")
            return _commande_interne_redirect(request)
        try:
            livraison_at = date.fromisoformat(livraison_at_str)
        except ValueError:
            messages.error(request, "Échéance invalide.")
            return _commande_interne_redirect(request)

        if not produit_id:
            messages.error(request, "Veuillez sélectionner un produit.")
            return _commande_interne_redirect(request)
        recette = _resoudre_recette(produit_id)
        if recette is None:
            messages.error(request, "Aucune recette n'est associée à ce produit.")
            return _commande_interne_redirect(request)

        # quantite_commander est absent du POST quand il est verrouillé côté
        # client (is_completed coché) : on conserve alors la valeur actuelle.
        if quantite_commander_str is not None and quantite_commander_str.strip():
            try:
                quantite_commander = Decimal(quantite_commander_str)
            except InvalidOperation:
                messages.error(request, "Quantité à commander invalide.")
                return _commande_interne_redirect(request)
            if quantite_commander <= 0:
                messages.error(request, "La quantité à commander doit être strictement supérieure à zéro.")
                return _commande_interne_redirect(request)
        else:
            quantite_commander = ci.quantite_commander

        if is_completed:
            try:
                quantite_produite = Decimal(quantite_produite_str) if quantite_produite_str else quantite_commander
            except InvalidOperation:
                quantite_produite = quantite_commander
        else:
            quantite_produite = Decimal('0')

        avant = model_to_dict(ci)
        ci.livraison_at = livraison_at
        ci.recette = recette
        ci.quantite_commander = quantite_commander
        ci.is_completed = is_completed
        ci.quantite_produite = quantite_produite
        ci.save()

        log_audit(
            AuditAction.UPDATE, f'Modification commande interne « {recette.produit.nom} »',
            table='CommandeInterne', record_id=ci.pk, old_value=avant, new_value=model_to_dict(ci),
        )
        messages.success(request, 'Commande interne modifiée avec succès.')
        return _commande_interne_redirect(request)


class CommandeInterneDeleteView(GroupRequiredMixin, View):
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        ci = CommandeInterne.objects.select_related('recette__produit').filter(pk=pk).first()
        if ci is None:
            messages.info(request, 'Cette commande interne a déjà été supprimée.')
            return _commande_interne_redirect(request)
        nom = ci.recette.produit.nom if ci.recette_id else '—'
        avant = model_to_dict(ci)
        try:
            ci.delete()
        except RestrictedError:
            messages.error(
                request,
                f"Impossible de supprimer cette commande interne (« {nom} ») : elle est "
                "encore liée à d'autres enregistrements.",
            )
            return _commande_interne_redirect(request)

        log_audit(
            AuditAction.DELETE, f'Suppression commande interne « {nom} »',
            table='CommandeInterne', record_id=pk, old_value=avant,
        )
        messages.success(request, 'Commande interne supprimée avec succès.')
        return _commande_interne_redirect(request)


class CommandeInterneBulkCreateView(GroupRequiredMixin, View):
    """Bouton « Ajout toutes les commandes » — les lignes invalides sont
    ignorées silencieusement, un décompte est affiché à la fin."""
    group_required = ['Administration', 'Production']
    template_name = 'production/commande_interne/list.html'

    def post(self, request):
        livraison_ats = request.POST.getlist('livraison_at')
        produit_ids   = request.POST.getlist('produit_id')
        quantites     = request.POST.getlist('quantite_commander')
        raw_rows = [
            {'livraison_at': l, 'produit_id': p, 'quantite_commander': q}
            for l, p, q in zip(livraison_ats, produit_ids, quantites)
        ]

        a_enregistrer = []
        for l, p, q in zip(livraison_ats, produit_ids, quantites):
            l = (l or '').strip()
            p = (p or '').strip()
            q = (q or '').strip()
            if not l or not p:
                continue
            try:
                livraison_at = date.fromisoformat(l)
            except ValueError:
                continue
            recette = _resoudre_recette(p)
            if recette is None:
                continue
            try:
                quantite = Decimal(q) if q else Decimal('0')
            except InvalidOperation:
                continue
            if quantite <= 0:
                continue
            a_enregistrer.append((recette, livraison_at, quantite))

        nb_ignorees = len(raw_rows) - len(a_enregistrer)

        if not a_enregistrer:
            messages.error(request, "Aucune ligne valide n'a été trouvée : rien n'a été enregistré.")
            next_url = _safe_next_url(request)
            if next_url:
                return redirect(next_url)
            context = _commande_interne_list_context(request)
            context.update(_commande_interne_part2_context(part2_rows_json=json.dumps(raw_rows)))
            return render(request, self.template_name, context)

        with transaction.atomic():
            created = [
                CommandeInterne.objects.create(
                    recette=recette, livraison_at=livraison_at, quantite_commander=quantite,
                )
                for recette, livraison_at, quantite in a_enregistrer
            ]

        log_audit(
            AuditAction.CREATE, f'Création groupée de {len(created)} commande(s) interne(s)',
            table='CommandeInterne', record_id=','.join(str(c.pk) for c in created),
            new_value={'commandes': [model_to_dict(c) for c in created]},
        )

        message = f'{len(created)} commande(s) enregistrée(s) avec succès.'
        if nb_ignorees:
            message += f' {nb_ignorees} ligne(s) ignorée(s) (champs manquants ou quantité invalide).'
        messages.success(request, message)
        next_url = _safe_next_url(request)
        return redirect(next_url) if next_url else redirect('production:commande-interne-list')


class CommandeInterneStockUpdateView(GroupRequiredMixin, View):
    """Bouton « Mise à jour du stock » — actif uniquement si is_completed est
    coché et is_stock_maj décoché (revérifié ici, pas seulement côté template).
    Délègue l'ensemble de la procédure à
    services.appliquer_mise_a_jour_stock_commande_interne() (voir sa docstring) ;
    ne fait ici que la lecture de la CommandeInterne, l'audit et les messages.
    Tout échec annule l'ensemble (transaction.atomic()) ; is_stock_maj reste
    False pour permettre de réessayer après correction des données.
    """
    group_required = ['Administration', 'Production']

    def post(self, request, pk):
        ci = get_object_or_404(CommandeInterne.objects.select_related('recette__produit'), pk=pk)
        produit = ci.recette.produit if ci.recette_id else None

        avant_ci = model_to_dict(ci)
        avant_produit = model_to_dict(produit) if produit else None
        avant_matiere = None
        if produit and produit.is_produit_semi_fini:
            matiere_avant = MatierePremiere.objects.filter(produit_id=produit.pk).first()
            avant_matiere = model_to_dict(matiere_avant) if matiere_avant else None

        try:
            with transaction.atomic():
                resultat = appliquer_mise_a_jour_stock_commande_interne(
                    produit.pk if produit else None,
                    ci.quantite_commander,
                    ci.quantite_produite,
                    is_completed=ci.is_completed,
                    is_stock_maj=ci.is_stock_maj,
                )
                ci.is_stock_maj = True
                ci.save(update_fields=['is_stock_maj'])
        except MiseAJourStockError as exc:
            messages.error(request, f"Mise à jour du stock impossible : {exc}")
            return _commande_interne_redirect(request)

        consommation = resultat['consommation']
        matiere_semi_finie = resultat['matiere_premiere_semi_finie']

        new_value = {
            'commande_interne': model_to_dict(ci),
            'matieres_premieres_consommees': {str(k): str(v) for k, v in consommation.items()},
        }
        if matiere_semi_finie is not None:
            new_value['matiere_premiere_semi_finie'] = model_to_dict(matiere_semi_finie)
            resultat_message = (
                f'Quantité de « {matiere_semi_finie.nom} » (matière première) augmentée '
                f'de {ci.quantite_produite}.'
            )
        else:
            new_value['produit'] = model_to_dict(resultat['produit'])
            resultat_message = f"Stock de « {produit.nom} » augmenté de {ci.quantite_produite}."

        log_audit(
            AuditAction.UPDATE, f'Mise à jour stock suite commande interne « {produit.nom} »',
            table='CommandeInterne', record_id=ci.pk,
            old_value={
                'commande_interne': avant_ci,
                'produit': avant_produit,
                'matiere_premiere_semi_finie': avant_matiere,
            },
            new_value=new_value,
        )
        messages.success(
            request,
            f'{resultat_message} {len(consommation)} matière(s) première(s) décrémentée(s).',
        )
        return _commande_interne_redirect(request)
