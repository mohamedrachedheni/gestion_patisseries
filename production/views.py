from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import F, RestrictedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import TemplateView, View

from core.mixins import GroupRequiredMixin

from .forms import ProduitForm
from .models import Produit


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

        messages.success(request, f'Produit « {nom} » supprimé avec succès.')
        return redirect('production:produit-list')
