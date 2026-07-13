from django.urls import path

from . import views

app_name = 'administration'

urlpatterns = [
    path('', views.HomeView.as_view(), name='home'),

    # Employés
    path('employes/',                         views.EmployeListView.as_view(),   name='employe-list'),
    path('employes/nouveau/',                 views.EmployeCreateView.as_view(), name='employe-create'),
    path('employes/<hashid:pk>/',             views.EmployeDetailView.as_view(), name='employe-detail'),
    path('employes/<hashid:pk>/modifier/',    views.EmployeUpdateView.as_view(), name='employe-update'),
    path('employes/<hashid:pk>/supprimer/',   views.EmployeDeleteView.as_view(), name='employe-delete'),

    # Transactions (lecture seule)
    path('transactions/', views.TransactionListView.as_view(), name='transaction-list'),

    # Transferts
    path('transferts/',                       views.TransfereListView.as_view(),   name='transfere-list'),
    path('transferts/nouveau/',               views.TransfereCreateView.as_view(), name='transfere-create'),
    path('transferts/<hashid:pk>/modifier/',  views.TransfereUpdateView.as_view(), name='transfere-update'),
    path('transferts/<hashid:pk>/supprimer/', views.TransfereDeleteView.as_view(), name='transfere-delete'),

    # Stock
    path('rupture-stock/',                views.RuptureStockView.as_view(),            name='rupture-stock-list'),
    path('rupture-stock/pdf/',             views.RuptureStockPdfView.as_view(),         name='rupture-stock-pdf'),
    path('rupture-stock/pdf-commandes/',   views.RuptureStockCommandesPdfView.as_view(), name='rupture-stock-commandes-pdf'),
]
