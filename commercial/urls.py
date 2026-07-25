from django.urls import path

from . import views

app_name = 'commercial'

urlpatterns = [
    path('', views.HomeView.as_view(), name='home'),

    # Clients
    path('clients/',                          views.ClientListView.as_view(),        name='client-list'),
    path('clients/nouveau/',                  views.ClientCreateView.as_view(),      name='client-create'),
    path('clients/sans-bon-livraison/',       views.ClientSansBLListView.as_view(),  name='client-sans-bl-list'),
    path('clients/<hashid:pk>/',              views.ClientDetailView.as_view(),      name='client-detail'),
    path('clients/<hashid:pk>/modifier/',     views.ClientUpdateView.as_view(),      name='client-update'),
    path('clients/<hashid:pk>/supprimer/',    views.ClientDeleteView.as_view(),      name='client-delete'),

    # Bons de livraison
    path('bons-livraison/',                     views.BonLivraisonListView.as_view(),         name='bon-livraison-list'),
    path('bons-livraison/nouveau/',             views.BonLivraisonCreateView.as_view(),       name='bon-livraison-create'),
    path('bons-livraison/numero-suivant/',      views.BonLivraisonNumeroSuivantView.as_view(), name='bon-livraison-numero-suivant'),
    path('bons-livraison/valeurs-defaut/',      views.BonLivraisonDefaultsView.as_view(),     name='bon-livraison-defaults'),
    path('bons-livraison/<hashid:pk>/',         views.BonLivraisonDetailView.as_view(),       name='bon-livraison-detail'),
    path('bons-livraison/<hashid:pk>/modifier/', views.BonLivraisonUpdateView.as_view(),      name='bon-livraison-update'),
    path('bons-livraison/<hashid:pk>/supprimer/', views.BonLivraisonDeleteView.as_view(),     name='bon-livraison-delete'),
    path('bons-livraison/<hashid:pk>/etat-pdf/', views.BonLivraisonPdfView.as_view(),         name='bon-livraison-pdf'),

    # Fournisseurs
    path('fournisseurs/',                       views.FournisseurListView.as_view(),   name='fournisseur-list'),
    path('fournisseurs/nouveau/',               views.FournisseurCreateView.as_view(), name='fournisseur-create'),
    path('fournisseurs/<hashid:pk>/',           views.FournisseurDetailView.as_view(), name='fournisseur-detail'),
    path('fournisseurs/<hashid:pk>/modifier/',  views.FournisseurUpdateView.as_view(), name='fournisseur-update'),
    path('fournisseurs/<hashid:pk>/supprimer/', views.FournisseurDeleteView.as_view(), name='fournisseur-delete'),

    # Achats
    path('achats/',                        views.AchatListView.as_view(),          name='achat-list'),
    path('achats/nouveau/',                views.AchatCreateView.as_view(),        name='achat-create'),
    path('achats/numero-suivant/',         views.AchatNumeroSuivantView.as_view(), name='achat-numero-suivant'),
    path('achats/<hashid:pk>/supprimer/',  views.AchatDeleteView.as_view(),        name='achat-delete'),
    path('achats/<hashid:pk>/modifier/',   views.AchatUpdateView.as_view(),        name='achat-update'),

    # Dépenses
    path('depenses/',                        views.DepenseListView.as_view(),          name='depense-list'),
    path('depenses/nouveau/',                views.DepenseCreateView.as_view(),        name='depense-create'),
    path('depenses/numero-suivant/',         views.DepenseNumeroSuivantView.as_view(), name='depense-numero-suivant'),
    path('depenses/<hashid:pk>/modifier/',   views.DepenseUpdateView.as_view(),        name='depense-update'),
    path('depenses/<hashid:pk>/supprimer/',  views.DepenseDeleteView.as_view(),        name='depense-delete'),

    # Agenda
    path('agenda/',                     views.AgendaListView.as_view(),   name='agenda-list'),
    path('agenda/ajouter/',             views.AgendaCreateView.as_view(), name='agenda-create'),
    path('agenda/<hashid:pk>/modifier/', views.AgendaUpdateView.as_view(), name='agenda-update'),
    path('agenda/<hashid:pk>/supprimer/', views.AgendaDeleteView.as_view(), name='agenda-delete'),
]
