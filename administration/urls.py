from django.urls import path

from . import views

app_name = 'administration'

urlpatterns = [
    path('', views.HomeView.as_view(), name='home'),

    # Gouvernorats
    path('gouvernorats/',                       views.GouvernoratListView.as_view(),   name='gouvernorat-list'),
    path('gouvernorats/nouveau/',               views.GouvernoratCreateView.as_view(), name='gouvernorat-create'),
    path('gouvernorats/<hashid:pk>/modifier/',  views.GouvernoratUpdateView.as_view(), name='gouvernorat-update'),
    path('gouvernorats/<hashid:pk>/supprimer/', views.GouvernoratDeleteView.as_view(), name='gouvernorat-delete'),

    # Délégations
    path('delegations/',                       views.DelegationListView.as_view(),   name='delegation-list'),
    path('delegations/nouveau/',               views.DelegationCreateView.as_view(), name='delegation-create'),
    path('delegations/<hashid:pk>/modifier/',  views.DelegationUpdateView.as_view(), name='delegation-update'),
    path('delegations/<hashid:pk>/supprimer/', views.DelegationDeleteView.as_view(), name='delegation-delete'),

    # Zones
    path('zones/',                       views.ZoneListView.as_view(),   name='zone-list'),
    path('zones/nouveau/',               views.ZoneCreateView.as_view(), name='zone-create'),
    path('zones/<hashid:pk>/modifier/',  views.ZoneUpdateView.as_view(), name='zone-update'),
    path('zones/<hashid:pk>/supprimer/', views.ZoneDeleteView.as_view(), name='zone-delete'),

    # Jours non ouvrés
    path('jours-non-ouvres/',                       views.JourNonOuvreListView.as_view(),   name='jour-non-ouvre-list'),
    path('jours-non-ouvres/nouveau/',               views.JourNonOuvreCreateView.as_view(), name='jour-non-ouvre-create'),
    path('jours-non-ouvres/<hashid:pk>/modifier/',  views.JourNonOuvreUpdateView.as_view(), name='jour-non-ouvre-update'),
    path('jours-non-ouvres/<hashid:pk>/supprimer/', views.JourNonOuvreDeleteView.as_view(), name='jour-non-ouvre-delete'),

    # Employés
    path('employes/',                         views.EmployeListView.as_view(),   name='employe-list'),
    path('employes/nouveau/',                 views.EmployeCreateView.as_view(), name='employe-create'),
    path('employes/<hashid:pk>/',             views.EmployeDetailView.as_view(), name='employe-detail'),
    path('employes/<hashid:pk>/modifier/',    views.EmployeUpdateView.as_view(), name='employe-update'),
    path('employes/<hashid:pk>/supprimer/',   views.EmployeDeleteView.as_view(), name='employe-delete'),

    # Transactions (lecture seule)
    path('transactions/',                            views.TransactionListView.as_view(),        name='transaction-list'),
    path('transactions/<hashid:pk>/detail-popup/',   views.TransactionDetailPopupView.as_view(), name='transaction-detail-popup'),

    # Solde compte caisse
    path('soldes-caisse/',                        views.HistoriqueSoldeCompteListView.as_view(),         name='solde-caisse-list'),
    path('soldes-caisse/<hashid:pk>/transactions/', views.HistoriqueSoldeCompteTransactionsView.as_view(), name='solde-caisse-transactions'),
    path('solde-caisse/',                         views.SoldeCaisseView.as_view(),                       name='solde-caisse'),

    # Transferts
    path('transferts/',                       views.TransfereListView.as_view(),   name='transfere-list'),
    path('transferts/nouveau/',               views.TransfereCreateView.as_view(), name='transfere-create'),
    path('transferts/<hashid:pk>/modifier/',  views.TransfereUpdateView.as_view(), name='transfere-update'),
    path('transferts/<hashid:pk>/supprimer/', views.TransfereDeleteView.as_view(), name='transfere-delete'),

    # Stock
    path('rupture-stock/',                views.RuptureStockView.as_view(),            name='rupture-stock-list'),
    path('rupture-stock/pdf/',             views.RuptureStockPdfView.as_view(),         name='rupture-stock-pdf'),
    path('rupture-stock/pdf-commandes/',   views.RuptureStockCommandesPdfView.as_view(), name='rupture-stock-commandes-pdf'),

    # Production
    path('demande-reference-produits/', views.DemandeReferenceProduitsView.as_view(), name='demande-reference-produits-list'),
    path('cout-production/',           views.CoutProductionListView.as_view(),         name='cout-production-list'),
    path('chiffre-affaire-produit/',   views.ChiffreAffaireParProduitView.as_view(),   name='chiffre-affaire-produit-list'),
    path(
        'chiffre-affaire-produit/graphique/<slug:champ>/pdf/',
        views.ChiffreAffaireProduitGraphiquePdfView.as_view(),
        name='chiffre-affaire-produit-graphique-pdf',
    ),
    path(
        'chiffre-affaire-commercial/',
        views.ChiffreAffaireParCommercialView.as_view(),
        name='chiffre-affaire-commercial-list',
    ),
    path(
        'tableau-bord-commerciaux/',
        views.TableauDeBordCommerciauxView.as_view(),
        name='tableau-bord-commerciaux-list',
    ),
    path(
        'chiffre-affaire-zone/',
        views.ChiffreAffaireParZoneView.as_view(),
        name='chiffre-affaire-zone-list',
    ),
]
