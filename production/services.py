"""Logique métier de production réutilisable, indépendante des vues.

Contient la procédure « Mise à jour du stock matière première suite à une
commande interne réalisée ».
"""
from decimal import ROUND_HALF_UP, Decimal

from .models import MatierePremiere, Produit, Recette, RecetteDetaille


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
