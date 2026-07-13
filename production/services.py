"""Logique métier de production réutilisable, indépendante des vues.

Contient la procédure « Mise à jour du stock matière première suite à une
commande interne réalisée » ainsi que le calcul de rupture de stock par
période pour les commandes internes non achevées.
"""
from decimal import ROUND_HALF_UP, Decimal

from .models import CommandeInterne, MatierePremiere, Produit, Recette, RecetteDetaille


class MiseAJourStockError(Exception):
    """Levée quand la procédure de mise à jour du stock ne peut pas être
    exécutée (commande non éligible, produit sans recette, recette invalide).
    Rien n'est modifié en base lorsqu'elle est levée."""


def _quantite_utilisee(quantite_commander, quantite_ligne, quantite_recette):
    brute = (Decimal(quantite_commander) * quantite_ligne) / quantite_recette
    return brute.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)


def appliquer_mise_a_jour_stock_commande_interne(
    produit_id, quantite_commander, quantite_produite, is_completed=1, is_stock_maj=0,
):
    """Applique la procédure « Mise à jour du stock matière première suite à
    une commande interne réalisée » pour le produit `produit_id`.

    N'agit que si is_completed=1 et is_stock_maj=0 (les valeurs réelles de la
    CommandeInterne concernée doivent être passées par l'appelant) ; sinon
    lève MiseAJourStockError sans rien modifier.

    Étape 1 : ajoute `quantite_produite` au stock du produit concerné —
    au champ Produit.stock si ce n'est pas un produit semi-fini
    (is_produit_semi_fini=False), ou à la quantite de la MatierePremiere liée
    (MatierePremiere.produit_id = produit_id) si c'en est un.

    Étape 2 : pour chaque ligne de RecetteDetaille de la Recette du produit
    concerné, diminue la quantite de la matière première utilisée de
    (quantite_commander × quantite_matiere_premiere) / quantite_recette
    (plancher à zéro) — que cette matière première soit elle-même un produit
    semi-fini ou non, sans développer sa propre recette.

    Tout s'exécute à l'intérieur d'une transaction.atomic() côté appelant :
    en cas d'erreur, rien n'est modifié.

    Retourne un dict :
      {'matiere_premiere_semi_finie': MatierePremiere|None,
       'produit': Produit|None,
       'consommation': {matiere_premiere_id: Decimal consommé}}
    """
    if not (is_completed and not is_stock_maj):
        raise MiseAJourStockError(
            "La mise à jour du stock n'est possible que si la commande est "
            "achevée et que son stock n'a pas déjà été mis à jour."
        )

    if quantite_produite is None or quantite_produite <= 0:
        raise MiseAJourStockError(
            "La quantité produite doit être strictement supérieure à zéro pour "
            "mettre à jour le stock."
        )
    if quantite_commander is None or quantite_commander <= 0:
        raise MiseAJourStockError(
            "La quantité commandée doit être strictement supérieure à zéro pour "
            "calculer la consommation de matières premières."
        )

    produit = Produit.objects.filter(pk=produit_id).first()
    if produit is None:
        raise MiseAJourStockError("Produit introuvable.")

    matiere_semi_finie = None
    if produit.is_produit_semi_fini:
        matiere_semi_finie = MatierePremiere.objects.filter(produit_id=produit.pk).first()
        if matiere_semi_finie is None:
            raise MiseAJourStockError(
                f"« {produit.nom} » est un produit semi-fini mais n'a aucun "
                "enregistrement correspondant dans la table matière première."
            )

    recette = Recette.objects.filter(produit_id=produit_id).first()
    if recette is None:
        raise MiseAJourStockError(
            "Ce produit n'a pas de recette : impossible de calculer sa consommation de matières premières."
        )
    if not recette.quantite or recette.quantite <= 0:
        raise MiseAJourStockError(
            f"La recette « {produit.nom} » a une quantité de référence invalide (≤ 0)."
        )

    lignes = list(RecetteDetaille.objects.filter(recette=recette).select_related('matiere_premiere'))

    consommation = {}
    for ligne in lignes:
        if not ligne.quantite:
            continue
        mp = ligne.matiere_premiere
        quantite_utilisee = _quantite_utilisee(quantite_commander, ligne.quantite, recette.quantite)
        consommation[mp.pk] = consommation.get(mp.pk, Decimal('0')) + quantite_utilisee

    if matiere_semi_finie is not None:
        matiere_semi_finie.quantite = (matiere_semi_finie.quantite or Decimal('0')) + quantite_produite
        matiere_semi_finie.save(update_fields=['quantite'])
    else:
        produit.stock = produit.stock + int(quantite_produite.to_integral_value(rounding=ROUND_HALF_UP))
        produit.save(update_fields=['stock'])

    matieres = {mp.pk: mp for mp in MatierePremiere.objects.filter(pk__in=consommation.keys())}
    for mp_id, quantite_consommee in consommation.items():
        mp = matieres[mp_id]
        nouvelle_quantite = (mp.quantite or Decimal('0')) - quantite_consommee
        if nouvelle_quantite < 0:
            nouvelle_quantite = Decimal('0')
        mp.quantite = nouvelle_quantite
        mp.save(update_fields=['quantite'])

    return {
        'produit': produit if matiere_semi_finie is None else None,
        'matiere_premiere_semi_finie': matiere_semi_finie,
        'consommation': consommation,
    }


def calculer_besoins_matieres_premieres_periode(date_debut, date_fin):
    """Calcule, pour les CommandeInterne non achevées (is_completed=False) de
    PRODUITS FINIS (recette.produit.is_produit_semi_fini=False) dont
    livraison_at est dans [date_debut, date_fin], le besoin en matières
    premières d'après la Recette de chaque produit commandé. Les commandes
    internes de produits semi-finis eux-mêmes ne sont pas prises en compte.

    Pour chaque ligne de RecetteDetaille : si la matière première n'est pas
    elle-même un produit semi-fini, son besoin alimente `besoins` (matières
    premières « de base », celles à acheter). Si elle EST un produit
    semi-fini, elle est exclue de `besoins` (jamais développée via sa propre
    recette ici) et son besoin brut alimente `besoins_semi_finis` à la place
    — utilisé par le contrôle de rupture des produits semi-finis eux-mêmes.

    Ne modifie rien en base — lecture seule. Les erreurs (recette absente ou
    invalide) sont collectées sans interrompre le calcul : la commande
    concernée est simplement ignorée.

    Retourne (besoins, besoins_semi_finis, erreurs) :
      besoins = {matiere_premiere_id: Decimal quantité nécessaire} (matières
                 premières « de base » uniquement)
      besoins_semi_finis = {matiere_premiere_id: Decimal quantité nécessaire}
                 — besoin brut pour chaque produit semi-fini utilisé comme
                 ingrédient par une recette de produit fini de la période
      erreurs = liste de messages (str)
    """
    besoins_directs = {}
    besoins_semi_fini = {}
    erreurs = []

    commandes = (
        CommandeInterne.objects
        .filter(
            is_completed=False, livraison_at__gte=date_debut, livraison_at__lte=date_fin,
            recette__produit__is_produit_semi_fini=False,
        )
        .select_related('recette__produit')
    )

    for ci in commandes:
        if not ci.recette_id:
            erreurs.append(f"Commande interne #{ci.pk} : aucune recette associée, ignorée.")
            continue
        recette = ci.recette
        if not recette.quantite or recette.quantite <= 0:
            erreurs.append(
                f"La recette « {recette.produit.nom} » a une quantité de référence "
                "invalide (≤ 0) : commande ignorée."
            )
            continue
        quantite_commander = ci.quantite_commander or Decimal('0')
        if quantite_commander <= 0:
            continue

        for ligne in RecetteDetaille.objects.filter(recette=recette).select_related('matiere_premiere'):
            if not ligne.quantite:
                continue
            quantite_utilisee = _quantite_utilisee(quantite_commander, ligne.quantite, recette.quantite)
            mp = ligne.matiere_premiere
            if mp.produit_id is None:
                besoins_directs[mp.pk] = besoins_directs.get(mp.pk, Decimal('0')) + quantite_utilisee
            else:
                besoins_semi_fini[mp.pk] = besoins_semi_fini.get(mp.pk, Decimal('0')) + quantite_utilisee

    return besoins_directs, besoins_semi_fini, erreurs


def quantite_commandee_semi_finis_periode(date_debut, date_fin):
    """{produit_id: Decimal quantite_commander} pour les CommandeInterne non
    achevées de produits semi-finis (is_produit_semi_fini=True) dont
    livraison_at est dans [date_debut, date_fin]."""
    quantite_commandee = {}
    commandes = (
        CommandeInterne.objects
        .filter(
            is_completed=False, livraison_at__gte=date_debut, livraison_at__lte=date_fin,
            recette__produit__is_produit_semi_fini=True,
        )
        .select_related('recette__produit')
    )
    for ci in commandes:
        produit_id = ci.recette.produit_id
        quantite_commandee[produit_id] = quantite_commandee.get(produit_id, Decimal('0')) + (ci.quantite_commander or Decimal('0'))
    return quantite_commandee


def calculer_besoins_matieres_premieres_pour_semi_finis_periode(date_debut, date_fin):
    """Calcule les matières premières « de base » nécessaires pour produire
    les produits semi-finis requis par la période : ceux utilisés comme
    ingrédients des recettes de produits finis commandés (non achevés,
    calculer_besoins_matieres_premieres_periode), auxquels s'ajoutent ceux
    directement commandés (non achevés) dans la période.

    Pour chaque semi-fini concerné, la quantité totale à produire = besoin en
    tant qu'ingrédient + quantité commandée directement (sans tenir compte de
    son propre stock disponible — voir la Partie 2 du contrôle de rupture
    pour ce calcul-là). Cette quantité est développée RÉCURSIVEMENT via la
    Recette du semi-fini : les ingrédients qui sont eux-mêmes des semi-finis
    voient leur propre recette développée à leur tour (protection anti-cycle),
    jusqu'à n'obtenir que des matières premières de base.

    Ne modifie rien en base — lecture seule. Les erreurs (semi-fini sans
    recette, recette invalide, cycle détecté) sont collectées sans
    interrompre le calcul : l'ingrédient concerné est ignoré.

    Retourne (besoins, erreurs) :
      besoins = {matiere_premiere_id: Decimal quantité nécessaire}
      erreurs = liste de messages (str)
    """
    _, besoins_semi_fini, erreurs = calculer_besoins_matieres_premieres_periode(date_debut, date_fin)
    quantite_commandee_par_produit = quantite_commandee_semi_finis_periode(date_debut, date_fin)

    mps_par_produit = {
        mp.produit_id: mp
        for mp in MatierePremiere.objects.filter(produit_id__in=quantite_commandee_par_produit.keys())
    }

    quantite_a_produire = dict(besoins_semi_fini)
    for produit_id, quantite_commandee in quantite_commandee_par_produit.items():
        mp = mps_par_produit.get(produit_id)
        if mp is None:
            erreurs.append(
                f"Produit semi-fini #{produit_id} commandé mais sans enregistrement "
                "MatierePremiere correspondant : ignoré."
            )
            continue
        quantite_a_produire[mp.pk] = quantite_a_produire.get(mp.pk, Decimal('0')) + quantite_commandee

    besoins_directs = {}

    def _developper(mp_id, quantite, visites):
        if mp_id in visites:
            erreurs.append(
                "Cycle détecté dans les recettes de produits semi-finis "
                f"(matière première #{mp_id}) : ingrédient ignoré."
            )
            return
        visites = visites | {mp_id}

        mp = MatierePremiere.objects.filter(pk=mp_id).select_related('produit').first()
        if mp is None:
            return

        sous_recette = Recette.objects.filter(produit_id=mp.produit_id).first()
        if sous_recette is None:
            erreurs.append(
                f"Le produit semi-fini « {mp.nom} » n'a pas de recette : impossible de "
                "calculer les matières premières nécessaires à sa production."
            )
            return
        if not sous_recette.quantite or sous_recette.quantite <= 0:
            erreurs.append(
                f"La recette du produit semi-fini « {mp.nom} » a une quantité de référence "
                "invalide (≤ 0) : ingrédient ignoré."
            )
            return

        for ligne in RecetteDetaille.objects.filter(recette=sous_recette).select_related('matiere_premiere'):
            if not ligne.quantite:
                continue
            quantite_utilisee = _quantite_utilisee(quantite, ligne.quantite, sous_recette.quantite)
            ligne_mp = ligne.matiere_premiere
            if ligne_mp.produit_id is None:
                besoins_directs[ligne_mp.pk] = besoins_directs.get(ligne_mp.pk, Decimal('0')) + quantite_utilisee
            else:
                _developper(ligne_mp.pk, quantite_utilisee, visites)

    for mp_id, quantite in quantite_a_produire.items():
        if quantite <= 0:
            continue
        _developper(mp_id, quantite, set())

    return besoins_directs, erreurs


def recettes_produit_unite_map():
    """Liste de {produit_id, recette_id, unite} pour tous les produits ayant
    une recette — sert à peupler, côté JS, l'auto-remplissage (lecture seule)
    du champ Unité lors du choix d'un Produit dans les formulaires de
    commande interne (production/commandes-internes/ et le contrôle de
    rupture de stock)."""
    return [
        {'produit_id': r.produit_id, 'recette_id': r.pk, 'unite': r.unite or ''}
        for r in Recette.objects.all()
    ]
