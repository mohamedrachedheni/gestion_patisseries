"""Tables "de référence" (données maîtres — utilisateurs, groupes, géographie
commerciale, clients, fournisseurs, matières premières, recettes…), par
opposition aux tables transactionnelles (ventes, achats, bons de livraison...)
qui changent en continu et ne se prêtent pas à un aller-retour Excel.

Point d'entrée unique pour export_donnees_excel et import_donnees_excel : un
seul ordre de dépendances de clé étrangère à maintenir, dans un seul fichier,
plutôt que dupliqué (et susceptible de diverger) dans les deux commandes.
"""
from django.apps import apps
from django.contrib.auth.models import Group, Permission, User


def tables_reference():
    """Modèles des tables de référence, dans l'ordre où ils doivent être
    rechargés : chaque modèle apparaît après tous ceux qu'il référence par
    clé étrangère, pour que l'import puisse se faire table par table sans
    désactiver les contraintes FK."""
    Employe = apps.get_model('administration', 'Employe')
    Gouvernorat = apps.get_model('commercial', 'Gouvernorat')
    Delegation = apps.get_model('commercial', 'Delegation')
    Zone = apps.get_model('commercial', 'Zone')
    Client = apps.get_model('commercial', 'Client')
    ClientUser = apps.get_model('commercial', 'ClientUser')
    Fournisseur = apps.get_model('commercial', 'Fournisseur')
    JourVisiteClient = apps.get_model('commercial', 'JourVisiteClient')
    MatierePremiereFamille = apps.get_model('production', 'MatierePremiereFamille')
    Produit = apps.get_model('production', 'Produit')
    MatierePremiere = apps.get_model('production', 'MatierePremiere')
    Recette = apps.get_model('production', 'Recette')
    RecetteDetaille = apps.get_model('production', 'RecetteDetaille')

    return [
        Group,
        Permission,
        User,
        Group.permissions.through,   # table auth_group_permissions
        User.groups.through,         # table auth_user_groups
        Employe,
        Gouvernorat,
        Delegation,
        Zone,
        Client,
        ClientUser,
        Fournisseur,
        JourVisiteClient,
        MatierePremiereFamille,
        Produit,
        MatierePremiere,
        Recette,
        RecetteDetaille,
    ]


def nom_feuille(model):
    """Nom de table SQL, tronqué à 31 caractères (limite Excel pour les noms
    d'onglet) — appliqué identiquement à l'export et à l'import pour que les
    deux commandes retrouvent toujours la même feuille."""
    return model._meta.db_table[:31]
