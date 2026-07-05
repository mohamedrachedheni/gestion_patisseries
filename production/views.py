from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, RestrictedError
from django.forms.models import model_to_dict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import TemplateView, View

from core.audit import AuditAction, log_audit
from core.mixins import GroupRequiredMixin

from .forms import FamilleForm, MatierePremiereForm, ProduitForm, ProduitSemiFiniForm
from .models import MatierePremiere, MatierePremiereFamille, Produit


def _form_errors_text(form):
    return ' '.join(e for errors in form.errors.values() for e in errors) or 'Données invalides.'


class HomeView(GroupRequiredMixin, TemplateView):
    group_required = 'Production'
    template_name = 'production/home.html'


# ─── Produits ───────────────────────────────────────────────────────────────

class ProduitListView(GroupRequiredMixin, View):
    group_required = 'Production'
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
    group_required = 'Production'
    template_name = 'production/produit/detail.html'

    def get(self, request, pk):
        produit = get_object_or_404(Produit, pk=pk)
        return render(request, self.template_name, {'produit': produit})


class ProduitCreateView(GroupRequiredMixin, View):
    group_required = 'Production'
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
    group_required = 'Production'
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
    group_required = 'Production'
    template_name = 'production/produit/confirm_delete.html'

    def _get(self, pk):
        return get_object_or_404(Produit, pk=pk)

    def get(self, request, pk):
        return render(request, self.template_name, {'produit': self._get(pk)})

    def post(self, request, pk):
        produit = self._get(pk)
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
    group_required = 'Production'
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
    group_required = 'Production'

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
    group_required = 'Production'

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
    group_required = 'Production'

    def post(self, request, pk):
        instance = get_object_or_404(MatierePremiereFamille, pk=pk)
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
    group_required = 'Production'
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
    group_required = 'Production'
    template_name = 'production/matiere_premiere/detail.html'

    def get(self, request, pk):
        mp = get_object_or_404(MatierePremiere.objects.select_related('matiere_premiere_famille'), pk=pk)
        return render(request, self.template_name, {'matiere_premiere': mp})


class MatierePremiereCreateView(GroupRequiredMixin, View):
    group_required = 'Production'
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
    group_required = 'Production'
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
    group_required = 'Production'
    template_name = 'production/matiere_premiere/confirm_delete.html'

    def _get(self, pk):
        return get_object_or_404(MatierePremiere, pk=pk)

    def get(self, request, pk):
        return render(request, self.template_name, {'matiere_premiere': self._get(pk)})

    def post(self, request, pk):
        mp = self._get(pk)
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
    group_required = 'Production'
    template_name = 'production/produit_semi_fini/list.html'

    def get(self, request):
        return render(request, self.template_name, _produit_semi_fini_context(request))


class ProduitSemiFiniCreateView(GroupRequiredMixin, View):
    group_required = 'Production'

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
    group_required = 'Production'

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
    group_required = 'Production'

    def post(self, request, pk):
        mp = get_object_or_404(MatierePremiere.objects.select_related('produit'), pk=pk)
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
