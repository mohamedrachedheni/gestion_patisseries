from django.urls import path

from . import views

app_name = 'commercial'

urlpatterns = [
    path('', views.HomeView.as_view(), name='home'),

    # Achats
    path('achats/',                        views.AchatListView.as_view(),          name='achat-list'),
    path('achats/nouveau/',                views.AchatCreateView.as_view(),        name='achat-create'),
    path('achats/numero-suivant/',         views.AchatNumeroSuivantView.as_view(), name='achat-numero-suivant'),
    path('achats/<hashid:pk>/supprimer/',  views.AchatDeleteView.as_view(),        name='achat-delete'),
]
