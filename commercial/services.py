import base64
from datetime import timedelta
from io import BytesIO

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from core.models import JourNonOuvre

from .models import Agenda, BonLivraison, JourVisiteClient

TOLERANCE_JOURS = 1


def _jours_non_ouvres_livraison(date_debut, date_fin):
    return set(
        JourNonOuvre.objects.filter(
            date__gte=date_debut, date__lte=date_fin, concerne_livraison=True,
        ).values_list('date', flat=True)
    )


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

    Retourne une liste de dicts triés par date puis client :
    {'client', 'date_attendue', 'jour_semaine', 'statut' ('honoré'/'manqué'/'ignoré'), 'detail', 'bon_livraison'}
    """
    marge = timedelta(days=TOLERANCE_JOURS)
    jours_non_ouvres = _jours_non_ouvres_livraison(date_debut - marge, date_fin + marge)

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
        for date_attendue in encore_restantes:
            if date_attendue in agenda_realise:
                lignes_client.append({
                    'client': client, 'date_attendue': date_attendue,
                    'jour_semaine': date_attendue.weekday(),
                    'statut': 'honoré', 'detail': 'Visite réalisée (agenda, sans livraison)',
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
    manqués), les « ignorés » étant exclus du calcul comme du dénominateur).

    Retourne une liste de tuples (date, taux) couvrant CHAQUE jour de
    [date_debut, date_fin] — taux est `None` les jours sans aucune visite
    honorée/manquée attendue ce jour-là (jour non prévu pour ce commercial,
    ou entièrement couvert par un autre commercial), pour que le graphique
    interrompe la courbe plutôt que d'interpoler à tort sur ces jours."""
    par_jour = {}
    for ligne in lignes:
        if ligne['statut'] == 'ignoré':
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
