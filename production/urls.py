from django.urls import path

from . import views

app_name = 'production'

urlpatterns = [
    path('', views.HomeView.as_view(), name='home'),

    # Produits
    path('produits/',                      views.ProduitListView.as_view(),   name='produit-list'),
    path('produits/nouveau/',              views.ProduitCreateView.as_view(), name='produit-create'),
    path('produits/<hashid:pk>/',          views.ProduitDetailView.as_view(), name='produit-detail'),
    path('produits/<hashid:pk>/modifier/', views.ProduitUpdateView.as_view(), name='produit-update'),
    path('produits/<hashid:pk>/supprimer/', views.ProduitDeleteView.as_view(), name='produit-delete'),

    # Familles de matière première
    path('familles/',                        views.FamilleListView.as_view(),   name='famille-list'),
    path('familles/nouvelle/',               views.FamilleCreateView.as_view(), name='famille-create'),
    path('familles/<hashid:pk>/modifier/',   views.FamilleUpdateView.as_view(), name='famille-update'),
    path('familles/<hashid:pk>/supprimer/',  views.FamilleDeleteView.as_view(), name='famille-delete'),

    # Matières premières
    path('matieres-premieres/',                        views.MatierePremiereListView.as_view(),   name='matiere-premiere-list'),
    path('matieres-premieres/nouvelle/',               views.MatierePremiereCreateView.as_view(), name='matiere-premiere-create'),
    path('matieres-premieres/<hashid:pk>/',            views.MatierePremiereDetailView.as_view(), name='matiere-premiere-detail'),
    path('matieres-premieres/<hashid:pk>/modifier/',   views.MatierePremiereUpdateView.as_view(), name='matiere-premiere-update'),
    path('matieres-premieres/<hashid:pk>/supprimer/',  views.MatierePremiereDeleteView.as_view(), name='matiere-premiere-delete'),

    # Produits semi-finis (Produit + MatierePremiere)
    path('produits-semi-finis/',                       views.ProduitSemiFiniListView.as_view(),   name='produit-semi-fini-list'),
    path('produits-semi-finis/nouveau/',                views.ProduitSemiFiniCreateView.as_view(), name='produit-semi-fini-create'),
    path('produits-semi-finis/<hashid:pk>/modifier/',   views.ProduitSemiFiniUpdateView.as_view(), name='produit-semi-fini-update'),
    path('produits-semi-finis/<hashid:pk>/supprimer/',  views.ProduitSemiFiniDeleteView.as_view(), name='produit-semi-fini-delete'),

    # Recettes
    path('recettes/',                        views.RecetteListView.as_view(),   name='recette-list'),
    path('recettes/nouvelle/',               views.RecetteCreateView.as_view(), name='recette-create'),
    path('recettes/<hashid:pk>/modifier/',   views.RecetteUpdateView.as_view(), name='recette-update'),
    path('recettes/<hashid:pk>/supprimer/',  views.RecetteDeleteView.as_view(), name='recette-delete'),

    # Commandes internes
    path('commandes-internes/',                       views.CommandeInterneListView.as_view(),       name='commande-interne-list'),
    path('commandes-internes/ligne/nouvelle/',        views.CommandeInterneQuickCreateView.as_view(), name='commande-interne-quick-create'),
    path('commandes-internes/ajout-tout/',            views.CommandeInterneBulkCreateView.as_view(),  name='commande-interne-bulk-create'),
    path('commandes-internes/<hashid:pk>/modifier/',  views.CommandeInterneUpdateView.as_view(),      name='commande-interne-update'),
    path('commandes-internes/<hashid:pk>/supprimer/', views.CommandeInterneDeleteView.as_view(),      name='commande-interne-delete'),
    path('commandes-internes/<hashid:pk>/stock/',     views.CommandeInterneStockUpdateView.as_view(), name='commande-interne-stock-update'),
]
