from django.urls import path

from . import views

app_name = 'backups'

urlpatterns = [
    path('',                              views.SauvegardeListView.as_view(),     name='sauvegarde-list'),
    path('nouveau/',                      views.SauvegardeCreateView.as_view(),   name='sauvegarde-create'),
    path('<hashid:pk>/telecharger/',       views.SauvegardeDownloadView.as_view(), name='sauvegarde-download'),
    path('<hashid:pk>/restaurer/',         views.SauvegardeRestoreView.as_view(),  name='sauvegarde-restore'),
    path('<hashid:pk>/supprimer/',         views.SauvegardeDeleteView.as_view(),   name='sauvegarde-delete'),
]
