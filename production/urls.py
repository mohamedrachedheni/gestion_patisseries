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
]
