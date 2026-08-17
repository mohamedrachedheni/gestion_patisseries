import base64
import math
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from django.db.models import Count, Max, Min, Q, Sum

from core.models import JourNonOuvre

from .models import (
    JOUR_SEMAINE_CHOICES, AbsenceCommercial, Agenda, BonLivraison, BonLivraisonDetaille, JourVisiteClient,
)

TOLERANCE_JOURS = 1


def _jours_non_ouvres_livraison(date_debut, date_fin):
    return set(
        JourNonOuvre.objects.filter(
            date__gte=date_debut, date__lte=date_fin, concerne_livraison=True,
        ).values_list('date', flat=True)
    )


def prochaine_date_visite_attendue(client, a_partir_de, horizon_jours=60):
    """Prochaine date (à partir de `a_partir_de` inclus) où ce client a un
    jour de visite récurrent (JourVisiteClient) qui tombe un jour ouvré (hors
    JourNonOuvre, concerne_livraison=True). None si aucun jour de visite
    n'est défini pour ce client, ou si aucune date ouverte n'est trouvée dans
    les `horizon_jours` prochains jours (garde-fou, cas normalement
    impossible en pratique)."""
    jours_semaine = set(client.jours_visite.values_list('jour_semaine', flat=True))
    if not jours_semaine:
        return None
    horizon_fin = a_partir_de + timedelta(days=horizon_jours)
    jours_non_ouvres = _jours_non_ouvres_livraison(a_partir_de, horizon_fin)
    jour = a_partir_de
    while jour <= horizon_fin:
        if jour.weekday() in jours_semaine and jour not in jours_non_ouvres:
            return jour
        jour += timedelta(days=1)
    return None


def _dates_attendues(jours_semaine, date_debut, date_fin, jours_non_ouvres):
    dates = []
    jour = date_debut
    while jour <= date_fin:
        if jour.weekday() in jours_semaine and jour not in jours_non_ouvres:
            dates.append(jour)
        jour += timedelta(days=1)
    return dates


def calculer_adherence_visites(clients_qs, date_debut, date_fin, commercial_id=None):
    """Compare, pour chaque client de `clients_qs`, ses jours de visite
    récurrents (JourVisiteClient) aux bons de livraison réellement enregistrés
    sur [date_debut, date_fin].

    Règles (validées avec l'utilisateur) :
    - Les jours qui tombent dans un JourNonOuvre (concerne_livraison=True) sont
      exclus du planning attendu — ni honorés, ni manqués.
    - Correspondance exacte en priorité (livraison le jour même), puis
      tolérance de ±1 jour ; chaque bon de livraison ne peut valider qu'un
      seul jour attendu (jamais réutilisé), pour ne pas fausser les plannings
      denses ou les jours attendus consécutifs.
    - Entre deux jours attendus non honorés en compétition pour la même
      livraison décalée, le jour le plus ancien est prioritaire.
    - Complément Agenda (statut « Réaliser ») pour capter une visite réalisée
      sans bon de livraison, sans dupliquer l'information dans une table à part.
    - Un client sans aucun JourVisiteClient est hors périmètre de ce rapport
      (a_visiter_apres n'est volontairement pas utilisé ici).

    Si `commercial_id` est fourni, l'analyse se place du point de vue de CE
    commercial (cas d'un client rattaché à plusieurs commerciaux) : seules SES
    propres livraisons (BonLivraison.user) et SES propres actions Agenda
    comptent pour « honoré ». Si un autre commercial a livré ce client autour
    du jour attendu, ce jour est marqué « ignoré » (ni honoré, ni manqué) — il
    ne doit pas être compté à la charge de ce commercial, mais ne prouve pas
    non plus sa diligence. Sans `commercial_id` (vue « tous les commerciaux »),
    toute livraison du client compte, quel que soit l'auteur — comportement
    inchangé.

    Si le commercial évalué est enregistré absent (AbsenceCommercial) un jour
    attendu qui, sans ça, serait resté « manqué » (aucune preuve positive —
    livraison, tolérance, autre commercial, agenda — ne l'a expliqué), ce jour
    est marqué « absent » (ni honoré, ni manqué) plutôt que « manqué » : une
    preuve positive l'emporte toujours sur l'absence déclarée.

    Retourne une liste de dicts triés par date puis client :
    {'client', 'date_attendue', 'jour_semaine',
     'statut' ('honoré'/'manqué'/'ignoré'/'absent'), 'detail', 'bon_livraison'}
    """
    marge = timedelta(days=TOLERANCE_JOURS)
    jours_non_ouvres = _jours_non_ouvres_livraison(date_debut - marge, date_fin + marge)

    absences_commercial = set()
    if commercial_id is not None:
        for absence in AbsenceCommercial.objects.filter(
            user_id=commercial_id, date_debut__lte=date_fin, date_fin__gte=date_debut,
        ):
            jour = max(absence.date_debut, date_debut)
            fin_absence = min(absence.date_fin, date_fin)
            while jour <= fin_absence:
                absences_commercial.add(jour)
                jour += timedelta(days=1)

    clients = clients_qs.prefetch_related('jours_visite')
    resultats = []

    for client in clients:
        jours_semaine = set(client.jours_visite.values_list('jour_semaine', flat=True))
        if not jours_semaine:
            continue

        dates_attendues = _dates_attendues(jours_semaine, date_debut, date_fin, jours_non_ouvres)
        if not dates_attendues:
            continue

        livraisons = BonLivraison.objects.filter(
            bon_livraison_code__client=client,
            bon_livraison_code__bon_livraison_at__gte=date_debut - marge,
            bon_livraison_code__bon_livraison_at__lte=date_fin + marge,
        ).select_related('bon_livraison_code').order_by('bon_livraison_code__bon_livraison_at')

        # Livraisons imputables au commercial évalué (ou à tout le monde si
        # commercial_id est None) : pool utilisé pour déterminer « honoré ».
        # Dates couvertes par un AUTRE commercial : utilisé uniquement pour
        # distinguer « ignoré » de « manqué » quand commercial_id est fourni.
        bl_dispo_par_date = {}
        dates_autre_commercial = set()
        for bl in livraisons:
            jour_bl = bl.bon_livraison_code.bon_livraison_at
            if commercial_id is None or bl.user_id == commercial_id:
                bl_dispo_par_date.setdefault(jour_bl, []).append(bl.bon_livraison_code)
            else:
                dates_autre_commercial.add(jour_bl)

        agenda_qs = Agenda.objects.filter(
            client=client, status='Réaliser',
            echeance_at__gte=date_debut, echeance_at__lte=date_fin,
        )
        if commercial_id is not None:
            agenda_qs = agenda_qs.filter(user_id=commercial_id)
        agenda_realise = set(agenda_qs.values_list('echeance_at', flat=True))

        lignes_client = []

        # 1) correspondances exactes (jour attendu du plus ancien au plus récent)
        restantes = []
        for date_attendue in dates_attendues:
            livraisons_ce_jour = bl_dispo_par_date.get(date_attendue)
            if livraisons_ce_jour:
                bl = livraisons_ce_jour.pop(0)
                lignes_client.append({
                    'client': client, 'date_attendue': date_attendue,
                    'jour_semaine': date_attendue.weekday(),
                    'statut': 'honoré', 'detail': 'Livraison le jour même',
                    'bon_livraison': bl,
                })
            else:
                restantes.append(date_attendue)

        # 2) tolérance ±1 jour — une livraison déjà utilisée ne peut plus resservir
        toujours_restantes = []
        for date_attendue in restantes:
            honoree = False
            for offset in (-TOLERANCE_JOURS, TOLERANCE_JOURS):
                candidate = date_attendue + timedelta(days=offset)
                livraisons_ce_jour = bl_dispo_par_date.get(candidate)
                if livraisons_ce_jour:
                    bl = livraisons_ce_jour.pop(0)
                    lignes_client.append({
                        'client': client, 'date_attendue': date_attendue,
                        'jour_semaine': date_attendue.weekday(),
                        'statut': 'honoré', 'detail': f'Livraison décalée au {candidate:%d/%m/%Y}',
                        'bon_livraison': bl,
                    })
                    honoree = True
                    break
            if not honoree:
                toujours_restantes.append(date_attendue)

        # 3) livré par un autre commercial du même client → ignoré (ni honoré, ni manqué)
        encore_restantes = []
        if commercial_id is not None:
            for date_attendue in toujours_restantes:
                couvert_par_autrui = any(
                    (date_attendue + timedelta(days=offset)) in dates_autre_commercial
                    for offset in (-TOLERANCE_JOURS, 0, TOLERANCE_JOURS)
                )
                if couvert_par_autrui:
                    lignes_client.append({
                        'client': client, 'date_attendue': date_attendue,
                        'jour_semaine': date_attendue.weekday(),
                        'statut': 'ignoré', 'detail': 'Client livré ce jour par un autre commercial',
                        'bon_livraison': None,
                    })
                else:
                    encore_restantes.append(date_attendue)
        else:
            encore_restantes = toujours_restantes

        # 4) complément agenda (visite réalisée sans bon de livraison)
        toujours_sans_preuve = []
        for date_attendue in encore_restantes:
            if date_attendue in agenda_realise:
                lignes_client.append({
                    'client': client, 'date_attendue': date_attendue,
                    'jour_semaine': date_attendue.weekday(),
                    'statut': 'honoré', 'detail': 'Visite réalisée (agenda, sans livraison)',
                    'bon_livraison': None,
                })
            else:
                toujours_sans_preuve.append(date_attendue)

        # 5) commercial absent ce jour-là → absent (ni honoré, ni manqué) ;
        # sinon, faute de toute preuve positive, le jour reste manqué
        for date_attendue in toujours_sans_preuve:
            if date_attendue in absences_commercial:
                lignes_client.append({
                    'client': client, 'date_attendue': date_attendue,
                    'jour_semaine': date_attendue.weekday(),
                    'statut': 'absent', 'detail': 'Commercial absent ce jour-là',
                    'bon_livraison': None,
                })
            else:
                lignes_client.append({
                    'client': client, 'date_attendue': date_attendue,
                    'jour_semaine': date_attendue.weekday(),
                    'statut': 'manqué', 'detail': 'Aucune livraison ni visite enregistrée',
                    'bon_livraison': None,
                })

        resultats.extend(lignes_client)

    resultats.sort(key=lambda r: (r['date_attendue'], str(r['client'])))
    return resultats


def serie_quotidienne_adherence(lignes, date_debut, date_fin):
    """Regroupe les lignes d'un rapport d'adhérence (déjà évalué pour UN seul
    commercial via `calculer_adherence_visites(..., commercial_id=...)`) par
    date_attendue et calcule le taux d'adhérence du jour (honorés / (honorés +
    manqués), les « ignorés » et « absent » étant exclus du calcul comme du
    dénominateur).

    Retourne une liste de tuples (date, taux) couvrant CHAQUE jour de
    [date_debut, date_fin] — taux est `None` les jours sans aucune visite
    honorée/manquée attendue ce jour-là (jour non prévu pour ce commercial,
    entièrement couvert par un autre commercial, ou commercial absent), pour
    que le graphique interrompe la courbe plutôt que d'interpoler à tort sur
    ces jours."""
    par_jour = {}
    for ligne in lignes:
        if ligne['statut'] in ('ignoré', 'absent'):
            continue
        stats = par_jour.setdefault(ligne['date_attendue'], {'honore': 0, 'total': 0})
        stats['total'] += 1
        if ligne['statut'] == 'honoré':
            stats['honore'] += 1

    serie = []
    jour = date_debut
    while jour <= date_fin:
        stats = par_jour.get(jour)
        taux = round(100 * stats['honore'] / stats['total'], 1) if stats else None
        serie.append((jour, taux))
        jour += timedelta(days=1)
    return serie


def generer_graphique_adherence(series_par_commercial):
    """`series_par_commercial` : liste de (label, [(date, taux_ou_None), ...]),
    une série par commercial (voir serie_quotidienne_adherence). Génère un
    graphique en courbes — une par commercial — du taux d'adhérence quotidien,
    avec des repères d'axe adaptés à la durée de la période (AutoDateLocator +
    ConciseDateFormatter) afin que le résultat tienne sur une seule page quelle
    que soit l'étendue de la période. Retourne le PNG encodé en base64, prêt à
    être intégré via <img src="data:image/png;base64,...">, ou None si aucune
    série ne contient de point (rien à tracer)."""
    series_avec_donnees = [
        (label, points) for label, points in series_par_commercial
        if any(taux is not None for _, taux in points)
    ]
    if not series_avec_donnees:
        return None

    fig, ax = plt.subplots(figsize=(11, 5))
    couleurs = plt.get_cmap('tab10').colors

    for i, (label, points) in enumerate(series_avec_donnees):
        x = [d for d, _ in points]
        y = [t for _, t in points]
        ax.plot(
            x, y, marker='o', markersize=3, linewidth=1.5,
            color=couleurs[i % len(couleurs)], label=label,
        )

    ax.set_ylim(-5, 105)
    ax.set_ylabel("Taux d'adhérence (%)", fontsize=9)
    ax.set_title('Taux d\'adhérence quotidien par commercial', fontsize=11, fontweight='bold')
    ax.grid(True, linestyle='--', alpha=0.4)

    locator = mdates.AutoDateLocator(minticks=4, maxticks=12)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate()

    ax.legend(fontsize=8, loc='lower left', ncol=min(len(series_avec_donnees), 4))
    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format='png', dpi=150)
    plt.close(fig)
    buffer.seek(0)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def calculer_demande_reference_produits(date_debut, date_fin, produit_ids=None, jours_semaine=None):
    """Phase 3 : quantité de référence moyenne livrée par produit et par jour
    de semaine sur la période [date_debut, date_fin], pour programmer les
    commandes internes de production.

    - Ne retient que le DERNIER BonLivraison de chaque BonLivraisonCode (un
      bon révisé plusieurs fois ne doit être compté qu'une fois — même
      principe que administration.views.ChiffreAffaireParCommercialView).
    - Exclut les dates qui tombent dans un JourNonOuvre (concerne_livraison=True),
      à la fois du total livré ET du nombre de jours de ce jour de semaine
      dans la période — sinon elles tirent la moyenne vers le bas artificiellement.
    - `produit_ids` : restreint aux produits donnés (None ou vide = tous).
    - `jours_semaine` : ensemble des jours de semaine à retenir, 0=Lundi..6=Dimanche
      (None = tous les 7 jours).

    Retourne une liste de dicts triés par jour de semaine puis nom de produit :
    {'jour_semaine', 'produit', 'quantite_reference', 'stock_disponible'}
    """
    jours_non_ouvres = _jours_non_ouvres_livraison(date_debut, date_fin)

    nb_jours_par_semaine = {w: 0 for w in range(7)}
    jour = date_debut
    while jour <= date_fin:
        if jour not in jours_non_ouvres:
            nb_jours_par_semaine[jour.weekday()] += 1
        jour += timedelta(days=1)

    dernier_bl_id_par_code = (
        BonLivraison.objects
        .filter(
            bon_livraison_code__bon_livraison_at__gte=date_debut,
            bon_livraison_code__bon_livraison_at__lte=date_fin,
        )
        .exclude(bon_livraison_code__bon_livraison_at__in=jours_non_ouvres)
        .values('bon_livraison_code_id')
        .annotate(dernier_id=Max('id'))
        .values_list('dernier_id', flat=True)
    )

    details_qs = BonLivraisonDetaille.objects.filter(
        bon_livraison_id__in=dernier_bl_id_par_code,
        produit__isnull=False,
    ).select_related('bon_livraison__bon_livraison_code', 'produit')

    if produit_ids:
        details_qs = details_qs.filter(produit_id__in=produit_ids)

    totaux = {}
    produits_par_id = {}
    for detail in details_qs:
        jour_semaine = detail.bon_livraison.bon_livraison_code.bon_livraison_at.weekday()
        if jours_semaine is not None and jour_semaine not in jours_semaine:
            continue
        cle = (jour_semaine, detail.produit_id)
        totaux[cle] = totaux.get(cle, 0) + detail.quantite
        produits_par_id[detail.produit_id] = detail.produit

    libelle_jour = dict(JOUR_SEMAINE_CHOICES)

    resultats = []
    for (jour_semaine, produit_id), quantite_totale in totaux.items():
        nb_jours = nb_jours_par_semaine.get(jour_semaine, 0)
        if not nb_jours:
            continue
        produit = produits_par_id[produit_id]
        resultats.append({
            'jour_semaine': jour_semaine,
            'jour_semaine_label': libelle_jour[jour_semaine],
            'produit': produit,
            # Arrondi au supérieur : mieux vaut prévoir légèrement plus que
            # d'être en rupture sur la quantité de référence.
            'quantite_reference': math.ceil(quantite_totale / nb_jours),
            'stock_disponible': produit.stock,
        })

    resultats.sort(key=lambda r: (r['jour_semaine'], r['produit'].nom))
    return resultats


def detecter_clients_en_derive(
    clients_qs, aujourdhui,
    nb_livraisons_min=8, fenetre_recente_jours=30, fenetre_historique_jours=90,
    seuil_retard=1.5, seuil_baisse=0.5,
):
    """Détecte, parmi `clients_qs`, les clients dont le rythme de commande
    dévie de leur propre moyenne historique — deux signaux indépendants,
    complémentaires (un client peut déclencher l'un, l'autre, ou les deux) :

    1. Retard : intervalle moyen entre deux livraisons calculé sur
       l'historique complet du client (jamais Client.a_visiter_apres,
       volontairement écarté). Signalé si le nombre de jours écoulés depuis
       la dernière livraison dépasse `seuil_retard` fois cet intervalle.
    2. Baisse de volume : montant livré sur les `fenetre_recente_jours`
       derniers jours comparé au montant « attendu » proportionnellement à
       la moyenne des `fenetre_historique_jours` derniers jours (qui
       englobent la fenêtre récente — pas une période disjointe). Signalé si
       le ratio recent/attendu descend sous `seuil_baisse`.

    Un client avec moins de `nb_livraisons_min` livraisons (dernière révision
    de chaque BonLivraisonCode, tout l'historique) est exclu de l'analyse :
    pas assez d'historique pour une moyenne significative.

    Retourne une liste de dicts (clients en dérive uniquement), triés par
    urgence (les deux signaux actifs d'abord, puis par ancienneté de la
    dernière livraison) :
    {'client', 'nb_livraisons', 'derniere_livraison', 'jours_depuis_derniere',
     'intervalle_moyen', 'en_retard', 'ratio_retard',
     'montant_recent', 'montant_attendu_recent', 'en_baisse', 'ratio_volume_pct'}
    """
    date_debut_recent = aujourdhui - timedelta(days=fenetre_recente_jours - 1)
    date_debut_historique = aujourdhui - timedelta(days=fenetre_historique_jours - 1)

    dernier_bl_id_par_code = (
        BonLivraison.objects
        .filter(bon_livraison_code__client__in=clients_qs)
        .values('bon_livraison_code_id').annotate(dernier_id=Max('id')).values_list('dernier_id', flat=True)
    )

    stats_par_client = (
        BonLivraison.objects.filter(pk__in=dernier_bl_id_par_code)
        .values('bon_livraison_code__client_id')
        .annotate(
            nb_livraisons=Count('id'),
            premiere_livraison=Min('bon_livraison_code__bon_livraison_at'),
            derniere_livraison=Max('bon_livraison_code__bon_livraison_at'),
            montant_recent=Sum(
                'total_montant',
                filter=Q(bon_livraison_code__bon_livraison_at__gte=date_debut_recent),
            ),
            montant_historique=Sum(
                'total_montant',
                filter=Q(bon_livraison_code__bon_livraison_at__gte=date_debut_historique),
            ),
        )
    )

    clients_par_id = {c.pk: c for c in clients_qs.select_related('zone')}
    facteur_extrapolation = Decimal(fenetre_historique_jours) / Decimal(fenetre_recente_jours)

    resultats = []
    for stats in stats_par_client:
        client = clients_par_id.get(stats['bon_livraison_code__client_id'])
        if client is None or stats['nb_livraisons'] < nb_livraisons_min:
            continue

        premiere, derniere = stats['premiere_livraison'], stats['derniere_livraison']
        nb_intervalles = stats['nb_livraisons'] - 1
        intervalle_moyen = (derniere - premiere).days / nb_intervalles if nb_intervalles > 0 else None
        jours_depuis_derniere = (aujourdhui - derniere).days

        ratio_retard = None
        en_retard = False
        if intervalle_moyen:
            ratio_retard = jours_depuis_derniere / intervalle_moyen
            en_retard = ratio_retard > seuil_retard

        montant_recent = stats['montant_recent'] or Decimal('0')
        montant_historique = stats['montant_historique'] or Decimal('0')
        montant_attendu_recent = montant_historique / facteur_extrapolation

        ratio_volume = None
        en_baisse = False
        if montant_attendu_recent > 0:
            ratio_volume = montant_recent / montant_attendu_recent
            en_baisse = ratio_volume < seuil_baisse

        if not en_retard and not en_baisse:
            continue

        resultats.append({
            'client': client,
            'nb_livraisons': stats['nb_livraisons'],
            'derniere_livraison': derniere,
            'jours_depuis_derniere': jours_depuis_derniere,
            'intervalle_moyen': round(intervalle_moyen, 1) if intervalle_moyen else None,
            'en_retard': en_retard,
            'ratio_retard': round(ratio_retard, 2) if ratio_retard is not None else None,
            'montant_recent': montant_recent,
            'montant_attendu_recent': montant_attendu_recent,
            'en_baisse': en_baisse,
            'ratio_volume_pct': round(ratio_volume * 100) if ratio_volume is not None else None,
        })

    resultats.sort(key=lambda r: (not (r['en_retard'] and r['en_baisse']), -r['jours_depuis_derniere']))
    return resultats
