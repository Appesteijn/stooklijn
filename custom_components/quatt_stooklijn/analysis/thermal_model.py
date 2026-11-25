"""Online 1R1C thermal model with Recursive Least Squares parameter learning.

Physics:
    C × dT_in/dt = Q_hp + g × Q_solar + P_c − U × (T_in − T_out)

Where:
    U = heat loss coefficient (W/K)
    C = thermal capacity (Wh/K)
    g = solar gain factor (dimensionless)
    P_c = constante warmtewinst (W)

Discretised (timestep Δt hours):
    ΔT = Δt × [θ₁×(T_out−T_in) + θ₂×Q_solar + θ₃×Q_hp + θ₄]

    θ₁ = U/C,  θ₂ = g/C,  θ₃ = 1/C,  θ₄ = P_c/C

Waarom P_c erin zit: zonder die term klopt de warmtebalans in het stookseizoen
niet. Het balanspunt ligt bij dit soort huizen 3–4 K onder de kamertemperatuur,
dus er komt zo'n kilowatt binnen die het model niet kent: bewoners en
apparaten, maar ook een afwijking in de vermogensmeting of het verschil tussen
de woonkamersensor en het huisgemiddelde. Vandaar de neutrale naam. Zonder P_c
moet die fout ergens anders heen. Het U-anker houdt U vast, dus kwam hij in C
terecht. In oktober 2026 zakte C daardoor in tien stookdagen van 10.700 naar
7.400 Wh/K, terwijl de afkoelmeting van september τ ≈ 85–100 u gaf. Nagespeeld
met de echte RLS-toestand: met P_c blijft C staan en komt P_c zelf op ~1.050 W,
precies wat het balanspunt voorspelt. Ook 's nachts (zon = 0) zit die ~1 kW
erin, dus het is geen verkeerd toegewezen zonnewinst.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..const import CAPACITY_FREE_CATCHUP_K, DEFAULT_ROOM_SETPOINT

_LOGGER = logging.getLogger(__name__)

# Defaults for cold start (typical Dutch terraced house)
DEFAULT_U = 200.0       # W/K — heat loss coefficient
DEFAULT_C = 5000.0      # Wh/K — thermal capacity
DEFAULT_G_SOLAR = 5.0   # W/(W/m²) — effective window area × SHGC

# RLS tuning
RLS_FORGETTING = 0.998       # effective window ≈ 500 samples ≈ 3 weeks
RLS_INITIAL_COV = 1000.0     # large initial uncertainty
RLS_MIN_UPDATES = 48         # 2 days before model is considered converged

# Sanity bounds for learned parameters
U_MIN, U_MAX = 30.0, 800.0     # W/K
C_MIN, C_MAX = 500.0, 30000.0  # Wh/K
# g_solar: W heat gain per W/m² irradiance = effective_window_area_m2 × SHGC
# Typical house: 5-20 m² windows × 0.3-0.6 SHGC = 1.5-12 W/(W/m²)
G_MIN, G_MAX = 0.0, 20.0       # W/(W/m²)
# Constante warmtewinst. Mag licht negatief: een vermogensmeting die iets te
# hoog leest, ziet eruit als negatieve winst, en dat is informatie, geen fout.
P_C_MIN, P_C_MAX = -1000.0, 3000.0  # W

# Aantal parameters van het model: θ₁..θ₄. Opgeslagen modellen van vóór P_c
# hebben er drie; ``OnlineRCModel.from_dict`` vult de vierde aan.
N_PARAMS = 4
_P_C = 3  # index van θ₄

# Onzekerheid van θ₄ bij een opgeslagen model dat P_c nog niet kende. Gelijk
# aan die van θ₃, maar dan voor een term van deze grootte: P[3,3] =
# P[2,2] × schaal². Zo heeft P_c dezelfde relatieve vrijheid als C, en schiet
# hij niet weg bij de eerste stookuren. Bij het naspelen van oktober 2026
# convergeerde P_c hiermee in een paar dagen.
P_C_COV_SCALE_W = 1000.0

# Minimum delta-T between indoor/outdoor to include a sample (avoids
# numerical issues when the temperature difference is too small)
MIN_DT_INDOOR_OUTDOOR = 2.0  # °C

# Below this heat input, a sample carries no information about U.
#
# Why: with Q_hp ≈ 0 the θ₃ (1/C) term drops out of the regression, and θ₁ (U/C)
# and θ₂ (g/C) both end up explaining the same slow drift — they become
# interchangeable. RLS then picks an arbitrary split along that axis. Over a
# Dutch summer, with a ~500-sample forgetting window, that is every sample for
# months on end, and the learned U drifts far away from reality without any
# guard noticing: it stays inside U_MIN..U_MAX and `is_converged` stays true.
#
# The threshold sits well under real heating operation (this house needs
# ~1900 W at +10 °C outside), so it separates "off or trickling" from
# "genuinely heating" rather than cutting into useful winter data.
ANCHOR_MAX_Q_HP_W = 500.0  # W


@dataclass
class RLSEstimator:
    """Recursive Least Squares estimator with forgetting factor.

    Estimates θ in the linear model y = x'θ, updating one sample at a time.
    """

    n_params: int = 3
    forgetting: float = RLS_FORGETTING
    theta: np.ndarray = field(default=None)
    P: np.ndarray = field(default=None)
    n_updates: int = 0
    #: Aantal updates per parameter waarin die parameter écht meebewoog.
    #: Nodig omdat n_updates alleen zegt hoe vaak er iets is bijgewerkt, niet
    #: of de parameter waar je naar kijkt daarbij iets geleerd heeft. Zonder
    #: dit onderscheid meldt een installatie die in de zomer in gebruik wordt
    #: genomen na 48 uur "geconvergeerd", terwijl C nog op de fabriekswaarde
    #: staat en er geen enkel monster is geweest waaruit hij te leren viel.
    n_updates_per_param: np.ndarray = field(default=None)

    def __post_init__(self) -> None:
        if self.theta is None:
            self.theta = np.zeros(self.n_params)
        if self.P is None:
            self.P = np.eye(self.n_params) * RLS_INITIAL_COV
        # Expliciet float: het bevroren pad schrijft in-place (theta[active] =)
        # en zou op een int-array stilzwijgend afkappen. Het gewone pad bindt
        # theta opnieuw en heeft dat probleem niet — die asymmetrie is precies
        # het soort verschil dat je pas maanden later merkt.
        self.theta = np.asarray(self.theta, dtype=float)
        self.P = np.asarray(self.P, dtype=float)
        if self.n_updates_per_param is None:
            self.n_updates_per_param = np.zeros(self.n_params, dtype=int)

    def initialise_from_physics(
        self, U: float, C: float, g: float, p_c: float = 0.0
    ) -> None:
        """Set initial θ from physical parameters (cold start).

        ``p_c`` telt alleen bij een estimator met vier parameters.
        """
        theta = [U / C, g / C, 1.0 / C, p_c / C]
        self.theta = np.array(theta[: self.n_params])
        # Keep large P so new data quickly corrects defaults
        self.P = np.eye(self.n_params) * RLS_INITIAL_COV

    def update(
        self, x: np.ndarray, y: float, frozen: tuple[int, ...] = ()
    ) -> None:
        """One RLS step: x = feature vector (n_params,), y = measured ΔT.

        ``frozen`` names parameters this sample carries no information about.
        Ze houden hun waarde én hun covariantie. Beide zijn nodig:

        * De waarde, omdat een parameter met een (bijna) nulregressor tóch
          meebeweegt — de gain ``K`` pikt hem op via de covariantie met de
          andere parameters, zodra die niet meer nul is.
        * De covariantie, omdat de vergeetfactor ``P`` elke stap met 1/λ
          opblaast terwijl er in die richting niets is dat hem terugdrukt.
          Over een Nederlandse zomer is dat een factor ~19 op P[2,2]. Slaat de
          warmtevraag daarna aan, dan zet die opgeblazen onzekerheid de
          parameter in één klap ergens anders neer.

        Bevriezen gebeurt als Schmidt-Kalmanfilter ("consider parameters"):
        de gain van de bevroren parameters staat op nul, maar hun onzekerheid
        telt wél mee in de gain van de rest, en P wordt als geheel bijgewerkt
        met de Joseph-vorm. Die vorm houdt P symmetrisch en positief-definiet
        voor élke gain, ook een bewust suboptimale. De vergeetfactor werkt
        alleen op het actieve blok; het bevroren blok blijft staan.

        Tot v0.10.7-beta.7 bewoog alleen ``P[actief, actief]``, en bleven de
        kruistermen ``P[actief, bevroren]`` op hun oude grootte. Dan is P geen
        geldige covariantie meer: de noemer ``λ + x'Px`` kan negatief worden en
        de gain krijgt het verkeerde teken zodra de parameter weer meedoet.
        Nagespeeld op het echte logboek (08-09 t/m 21-11-2026): een jong model
        dat op 15-10 begon, had 373 uur een indefiniete P en stond 1% van de
        tijd online, met g = 58,7 aan het eind. Met deze update: 0 uur, 89%
        online. Zie ``TestCovariantieBlijftGeldig``.
        """
        lam = self.forgetting
        if frozen:
            active = [i for i in range(self.n_params) if i not in frozen]
            if not active:
                return
            Px = self.P @ x
            denom = lam + float(x @ Px)
            if abs(denom) < 1e-12:
                return  # numerical guard
            K = np.zeros(self.n_params)
            K[active] = Px[active] / denom
            err = y - float(x @ self.theta)
            self.theta = self.theta + K * err
            A = np.eye(self.n_params) - np.outer(K, x)
            P_new = A @ self.P @ A.T + lam * np.outer(K, K)
            block = np.ix_(active, active)
            P_new[block] = P_new[block] / lam
            self.P = (P_new + P_new.T) / 2.0
            self.n_updates += 1
            self.n_updates_per_param[active] += 1
            return

        Px = self.P @ x
        denom = lam + float(x @ Px)
        if abs(denom) < 1e-12:
            return  # numerical guard
        K = Px / denom
        err = y - float(x @ self.theta)
        self.theta = self.theta + K * err
        self.P = (self.P - np.outer(K, Px)) / lam
        self.n_updates += 1
        self.n_updates_per_param += 1

    @property
    def is_converged(self) -> bool:
        return bool(
            self.n_updates >= RLS_MIN_UPDATES
            and self.n_updates_per_param.min() >= RLS_MIN_UPDATES
        )

    def to_dict(self) -> dict:
        return {
            "theta": self.theta.tolist(),
            "P": self.P.tolist(),
            "n_updates": self.n_updates,
            "n_updates_per_param": self.n_updates_per_param.tolist(),
            "forgetting": self.forgetting,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RLSEstimator:
        est = cls(
            n_params=len(data["theta"]),
            forgetting=data.get("forgetting", RLS_FORGETTING),
        )
        est.theta = np.asarray(data["theta"], dtype=float)
        est.P = np.asarray(data["P"], dtype=float)
        est.n_updates = data.get("n_updates", 0)
        # Bestaande modellen kennen het veld niet. Terugvallen op n_updates
        # gaat uit van het oude gedrag — alles bewoog altijd mee — zodat een
        # draaiende installatie niet plotseling naar de batch-terugval springt.
        per_param = data.get("n_updates_per_param")
        est.n_updates_per_param = (
            np.asarray(per_param, dtype=int) if per_param is not None
            else np.full(est.n_params, est.n_updates, dtype=int)
        )
        est.repair_covariance()
        return est

    def repair_covariance(self) -> bool:
        """Maak P weer een geldige covariantie als hij dat niet meer is.

        Voor opgeslagen toestanden van vóór v0.10.7-beta.7: de oude blokupdate
        kon P indefiniet maken, en de Joseph-vorm houdt een P die al
        indefiniet is ook indefiniet. Negatieve eigenwaarden worden afgekapt
        op een kleine positieve ondergrens; θ blijft staan. Een P met NaN of
        inf begint opnieuw op de startonzekerheid. Geeft True als er iets is
        hersteld.

        Getoetst op de correlatiematrix, niet op P zelf. De parameters liggen
        ordes van grootte uit elkaar (P[2,2] ~1e-9, met anker P[3,3] tot 1000),
        en een drempel relatief aan de grootste eigenwaarde las een gezonde,
        rijpe P als kapot: bij elke herstart een melding, en P[2,2] ~15%
        verschoven (gemeld 22-11-2026, kleinste eigenwaarde 6,75e-10).
        """
        P = self.P
        if not np.all(np.isfinite(P)):
            self.P = np.eye(self.n_params) * RLS_INITIAL_COV
            _LOGGER.warning("RC model: covariantie bevatte NaN/inf, opnieuw begonnen")
            return True
        P = (P + P.T) / 2.0
        repaired = False
        d = np.diag(P).copy()
        for i in np.flatnonzero(d <= 0):
            # Een variantie van nul of minder kan niet: die parameter leert
            # opnieuw, zonder kruistermen.
            P[i, :] = 0.0
            P[:, i] = 0.0
            P[i, i] = d[i] = RLS_INITIAL_COV
            repaired = True
        s = np.sqrt(d)
        R = P / np.outer(s, s)
        w, V = np.linalg.eigh(R)
        if w.min() > 1e-12 and not repaired:
            self.P = P
            return False
        if w.min() <= 1e-12:
            R = (V * np.maximum(w, 1e-9)) @ V.T
            P = R * np.outer(s, s)
        self.P = (P + P.T) / 2.0
        _LOGGER.warning(
            "RC model: covariantie was niet positief-definiet (kleinste "
            "eigenwaarde correlatie %.3g), hersteld", float(w.min()),
        )
        return True


class OnlineRCModel:
    """1R1C thermal model with online parameter learning."""

    def __init__(self) -> None:
        self._prev_t_indoor: float | None = None
        self._prev_t_outdoor: float | None = None
        self._prev_q_hp: float | None = None
        self._prev_q_solar: float | None = None
        self._prev_timestamp: datetime | None = None
        # Seasonal U from the batch heat-loss regression, used to hold U steady
        # while there is no heat input to identify it from. None = no anchor.
        self._u_prior: float | None = None
        # Whether the most recent update had its U held at the prior. Exposed
        # in `params` so an anchored model is distinguishable from a learned
        # one — otherwise a summer-frozen U looks exactly like a converged one.
        self._u_anchored: bool = False
        # Of P_c al een startwaarde uit het balanspunt heeft gekregen. Tot dan
        # staat θ₄ op nul en gedraagt het model zich als het oude 1R1C.
        self._p_c_seeded: bool = False
        # P_c in watt uit de warmteverliesanalyse, als die er is. Dan leert RLS
        # P_c niet zelf; zie set_p_c_anchor.
        self._p_c_anchor: float | None = None
        self._rls = RLSEstimator(n_params=N_PARAMS)
        # Initialise with reasonable defaults
        self._rls.initialise_from_physics(DEFAULT_U, DEFAULT_C, DEFAULT_G_SOLAR)

    def initialise_from_batch(self, U: float) -> None:
        """Use batch-estimated heat loss as better starting point."""
        if U_MIN <= U <= U_MAX:
            self._rls.initialise_from_physics(U, DEFAULT_C, DEFAULT_G_SOLAR)
            # θ₄ staat weer op nul, dus een eerdere startwaarde is weg.
            self._p_c_seeded = False
            _LOGGER.info(
                "RC model initialised from batch heat loss: U=%.1f W/K", U
            )

    def set_u_prior(self, U: float | None) -> None:
        """Set the seasonal U that low-heat samples fall back on.

        Comes from the batch heat-loss regression over a full season, which is
        the one estimate that *does* see cold weather with real heat input.
        Refresh it whenever that regression is recomputed — it improves as the
        window grows, and the anchor should track it rather than freeze on the
        first value ever seen.
        """
        if U is None:
            self._u_prior = None
            return
        if U_MIN <= U <= U_MAX:
            self._u_prior = float(U)
        else:
            _LOGGER.warning(
                "RC model: ignoring implausible U prior %.1f W/K "
                "(outside %.0f-%.0f)", U, U_MIN, U_MAX,
            )

    def seed_constant_gain(self, p_c: float | None) -> bool:
        """Geef P_c één keer een startwaarde, uit het balanspunt.

        De batchregressie zegt waar de warmtevraag nul wordt: bij T_bal. Daar
        dekt de constante winst precies het verlies, dus P_c = U × (T_in −
        T_bal). Zonder startwaarde begint P_c op nul en moet RLS de hele
        kilowatt zelf vinden; tot die tijd duwt de fout C weer weg.

        Maar één keer: daarna leert RLS P_c, en een tweede startwaarde zou dat
        overschrijven. Geeft True als er gezaaid is.
        """
        if self._p_c_seeded or p_c is None:
            return False
        if not P_C_MIN <= p_c <= P_C_MAX:
            _LOGGER.warning(
                "RC model: onwaarschijnlijke startwaarde P_c %.0f W genegeerd "
                "(buiten %.0f–%.0f)", p_c, P_C_MIN, P_C_MAX,
            )
            return False
        self._rls.theta[_P_C] = p_c * self._rls.theta[2]
        self._p_c_seeded = True
        _LOGGER.info("RC model: P_c gezaaid op %.0f W", p_c)
        return True

    def set_p_c_anchor(self, p_c: float | None) -> None:
        """Zet de constante warmtewinst vast op een waarde uit de warmteverliesanalyse.

        Waarom (gemeten 20-11-2026): vrij geleerd staat P_c in de RLS als
        θ₄ = P_c/C, en groeit hij in watt mee als C verschuift. Op het gemeten
        huis liep P_c op naar 1.406 W, terwijl het balanspunt ~970 W zegt; het
        1R1C voorspelde de kamer daardoor na 12 uur 0,7 °C te warm, en scoorde
        slechter dan "niets doen" (0,494 tegen 0,461 K).

        Met anker is P_c een bekende invoer: hij telt op bij het
        warmtepompvermogen in de regressor van θ₃ = 1/C, en θ₄ staat vast op
        nul zonder regressor. RLS leert dan alleen nog de dynamiek: U, g en C.
        Let op: daardoor is de regressor van θ₃ nooit bijna nul, ook niet in
        de uren waarin θ₃ bevroren wordt. Dat vraagt een update die de
        kruiscovarianties meeneemt (zie ``RLSEstimator.update``).

        Bij het aanzetten wordt θ₄ nul en verliest hij zijn kruiscovarianties;
        bij uitzetten neemt θ₄ de laatste ankerwaarde over en leert weer vrij.
        """
        if p_c is not None and not P_C_MIN <= p_c <= P_C_MAX:
            _LOGGER.warning(
                "RC model: onwaarschijnlijk anker P_c %.0f W genegeerd "
                "(buiten %.0f–%.0f)", p_c, P_C_MIN, P_C_MAX,
            )
            return
        was = getattr(self, "_p_c_anchor", None)
        rls = self._rls
        if p_c is not None and was is None:
            rls.theta[_P_C] = 0.0
            keep = rls.P[_P_C, _P_C]
            rls.P[_P_C, :] = 0.0
            rls.P[:, _P_C] = 0.0
            rls.P[_P_C, _P_C] = keep
            _LOGGER.info("RC model: P_c vastgezet op %.0f W (warmteverliesanalyse)", p_c)
        elif p_c is None and was is not None:
            rls.theta[_P_C] = was * rls.theta[2]
        self._p_c_anchor = float(p_c) if p_c is not None else None

    def _constant_gain_term(self, theta: np.ndarray) -> float:
        """Bijdrage van P_c aan dT/dt (K/u): θ₃·anker, of θ₄ als hij vrij leert."""
        anchor = getattr(self, "_p_c_anchor", None)
        return theta[2] * anchor if anchor is not None else theta[_P_C]

    def update(
        self,
        t_indoor: float,
        t_outdoor: float,
        q_hp_w: float,
        q_solar_w: float,
        timestamp: datetime,
        q_hp_interval_w: float | None = None,
    ) -> bool:
        """Add a measurement and update the model.

        Call this once per hour.  Returns True if the update was used.
        The RLS update uses the PREVIOUS hour's conditions (T_out, Q_hp,
        Q_solar) to explain the observed ΔT, since those are the conditions
        that caused the temperature change.

        ``q_hp_interval_w`` is de gemiddelde warmte over het interval sinds de
        vorige update (uit het 5-minutenlogboek). Gegeven, dan vervangt hij de
        momentopname van de vorige tik als verklaring van ΔT. Sinds
        v0.10.7-beta.6: een momentopname was op een huis met alleen een
        warmtepomp, die in stabiele blokken draait, gelijkwaardig (+27 W), maar
        mist de korte stoten van een bijspringende ketel, of telt een stoot van
        een paar minuten als een heel uur. Nagebootst (21-11-2026, radiatorhuis
        met ketel): met de momentopname 3 van de 20 modellen online, met het
        uurgemiddelde 16. ``q_hp_w`` blijft de momentopname, als terugval voor
        de volgende tik.
        """
        # Eén NaN of inf in θ is blijvend: RLS rekent hem bij elke volgende
        # update door. Zo'n monster dus helemaal niet gebruiken, ook niet als
        # "vorige" waarde voor de volgende tik.
        values = (t_indoor, t_outdoor, q_hp_w, q_solar_w)
        if q_hp_interval_w is not None:
            values += (q_hp_interval_w,)
        if not all(np.isfinite(v) for v in values):
            _LOGGER.warning(
                "RC model: monster met NaN/inf overgeslagen (T_in=%s, T_out=%s, "
                "Q=%s, zon=%s, Q_interval=%s)",
                t_indoor, t_outdoor, q_hp_w, q_solar_w, q_hp_interval_w,
            )
            return False

        if self._prev_t_indoor is None or self._prev_timestamp is None:
            # First call — store and wait for next
            self._prev_t_indoor = t_indoor
            self._prev_t_outdoor = t_outdoor
            self._prev_q_hp = q_hp_w
            self._prev_q_solar = q_solar_w
            self._prev_timestamp = timestamp
            return False

        # Calculate time step in hours
        dt_seconds = (timestamp - self._prev_timestamp).total_seconds()
        dt_hours = dt_seconds / 3600.0

        # Guard: only accept roughly hourly samples (0.5–2h)
        if dt_hours < 0.5 or dt_hours > 2.0:
            _LOGGER.info(
                "RC model: skipping, dt=%.1f hours (need 0.5–2h)",
                dt_hours,
            )
            self._prev_t_indoor = t_indoor
            self._prev_t_outdoor = t_outdoor
            self._prev_q_hp = q_hp_w
            self._prev_q_solar = q_solar_w
            self._prev_timestamp = timestamp
            return False

        # Guard: need meaningful temperature difference for identification
        dt_indoor_outdoor = abs(self._prev_t_indoor - self._prev_t_outdoor)
        if dt_indoor_outdoor < MIN_DT_INDOOR_OUTDOOR:
            _LOGGER.info(
                "RC model: skipping, |T_in-T_out|=%.1f°C < %.1f°C minimum",
                dt_indoor_outdoor, MIN_DT_INDOOR_OUTDOOR,
            )
            self._prev_t_indoor = t_indoor
            self._prev_t_outdoor = t_outdoor
            self._prev_q_hp = q_hp_w
            self._prev_q_solar = q_solar_w
            self._prev_timestamp = timestamp
            return False

        # Build observation using PREVIOUS conditions:
        # y = ΔT_indoor (what happened), x = Δt × [T_out-T_in, Q_solar, Q_hp, 1] (what caused it)
        delta_t = t_indoor - self._prev_t_indoor
        anchor = getattr(self, "_p_c_anchor", None)
        # De warmte die ΔT veroorzaakte: het gemiddelde over het interval als
        # dat er is, anders de momentopname van de vorige tik.
        q_interval = (
            float(q_hp_interval_w) if q_hp_interval_w is not None
            else (self._prev_q_hp or 0.0)
        )
        x = np.array([
            dt_hours * (self._prev_t_outdoor - self._prev_t_indoor),  # θ₁ term
            dt_hours * self._prev_q_solar,                            # θ₂ term
            # θ₃ term: met anker telt de bekende constante winst mee als warmte.
            dt_hours * (q_interval + (anchor or 0.0)),
            dt_hours if anchor is None else 0.0,                      # θ₄ term
        ])

        # Zonder warmte-input is C net zo onidentificeerbaar als U: de
        # regressor van θ₃ (1/C) ís dt·Q_hp. Vandaar dat θ₃ hier bevroren
        # wordt, waarde én covariantie — zie RLSEstimator.update.
        #
        # Dat dit nodig is, is niet vanzelfsprekend. Bij Q_hp exact nul staat
        # C uit zichzelf stil: x[2] = 0, P blijft diagonaal, de gain in die
        # richting blijft nul. Maar echte zomers zijn niet exact nul —
        # standby, tapwater, een enkele stookdag — en bij een paar honderd
        # watt onder de drempel loopt C in één seizoen volledig weg, aangejaagd
        # door een P[2,2] die de vergeetfactor intussen heeft opgeblazen.
        # g blijft wél leren: de zomer is juist de beste periode daarvoor.
        #
        # Met anker is de regressor van θ₃ dt·(Q_hp + P_c), dus nooit bijna
        # nul, ook in deze uren niet. Bevriezen kan dan alleen omdat
        # RLSEstimator.update de kruiscovarianties consistent bijwerkt
        # (Schmidt-Kalman, sinds v0.10.7-beta.7). De oude blokupdate liet ze
        # staan, en dan raakte P ontregeld: zie TestCovariantieBlijftGeldig.
        # θ₃ niet bevriezen is géén alternatief: nagespeeld op het echte
        # logboek vanaf 08-09-2026 liep C dan naar nul.
        #
        # θ₄ (P_c/C, alleen zonder anker) wordt niet mee bevroren. Op het
        # koude-starthuis van TestOnlineRCModel liep dat met de oude
        # blokupdate uit tot een negatieve C; met de nieuwe update is het niet
        # opnieuw geprobeerd. Prijs van meeleren: P_c volgt ook de zomer, en
        # moet in het najaar terug. Zie de CHANGELOG.
        low_heat = q_interval < ANCHOR_MAX_Q_HP_W

        frozen: tuple[int, ...] = (2,) if low_heat else ()
        if anchor is not None:
            # θ₄ staat vast op nul en heeft geen regressor: bevriezen kost niets.
            frozen = frozen + (_P_C,)
        self._rls.update(x, delta_t, frozen=frozen)

        # Hold U at the seasonal value when this sample could not have said
        # anything about it — de U/g-degeneratie mag U niet verplaatsen.
        #
        # Deliberately a hard projection rather than a weighted prior: a soft
        # pull needs a strength constant, and there is no principled value for
        # it. "This sample carries no U information" is a yes/no property, so
        # the correction is too. θ₁ = U/C and θ₃ = 1/C, so pinning U means
        # θ₁ := U_prior · θ₃. De covariantie van θ₁ blijft meelopen, zodat RLS
        # U weer vrij kan bewegen zodra er echt gestookt wordt.
        self._u_anchored = self._u_prior is not None and low_heat
        if self._u_anchored:
            self._rls.theta[0] = self._u_prior * self._rls.theta[2]

        self._prev_t_indoor = t_indoor
        self._prev_t_outdoor = t_outdoor
        self._prev_q_hp = q_hp_w
        self._prev_q_solar = q_solar_w
        self._prev_timestamp = timestamp
        return True

    def predict_t_indoor(
        self,
        t_indoor: float,
        t_outdoor: float,
        q_hp_w: float,
        q_solar_w: float,
        dt_hours: float = 1.0,
    ) -> float:
        """Predict indoor temperature after dt_hours."""
        theta = self._rls.theta
        delta_t = dt_hours * (
            theta[0] * (t_outdoor - t_indoor)
            + theta[1] * q_solar_w
            + theta[2] * q_hp_w
            + self._constant_gain_term(theta)
        )
        return t_indoor + delta_t

    def calc_required_power(
        self,
        t_indoor: float,
        t_outdoor: float,
        q_solar_w: float,
        t_setpoint: float,
        dt_hours: float = 1.0,
    ) -> float:
        """Calculate HP power (W) needed to reach t_setpoint after dt_hours.

        Returns 0 if no heating needed (house warm enough or solar sufficient).
        """
        theta = self._rls.theta
        if abs(theta[2]) < 1e-12:
            return 0.0  # model not yet usable

        # Solve: t_setpoint = t_indoor + dt × [θ₁(T_out-T_in) + θ₂×Q_solar + θ₃×Q_hp + θ₄]
        # => Q_hp = (t_setpoint - t_indoor - dt×θ₁×(T_out-T_in) - dt×θ₂×Q_solar - dt×θ₄) / (dt×θ₃)
        numerator = (
            t_setpoint
            - t_indoor
            - dt_hours * theta[0] * (t_outdoor - t_indoor)
            - dt_hours * theta[1] * q_solar_w
            - dt_hours * self._constant_gain_term(theta)
        )
        q_hp = numerator / (dt_hours * theta[2])
        return max(0.0, q_hp)

    def _n_updates_min(self) -> int:
        """Updates van de traagste parameter die het model zelf leert.

        Met anker leert θ₄ niet: hij staat bij elke update bevroren, dus zijn
        teller loopt niet op. Telt hij mee, dan haalt een model dat met anker
        begint (nieuwe installatie, of minder dan RLS_MIN_UPDATES uur oud bij
        het aanzetten) nooit de drempel, en blijft het voorgoed op de
        batch-terugval (v0.10.7-beta.2).
        """
        counts = self._rls.n_updates_per_param
        if getattr(self, "_p_c_anchor", None) is not None:
            counts = np.delete(counts, _P_C)
        return int(counts.min())

    @property
    def is_broken(self) -> bool:
        """Is de schatting ontspoord tot iets waar niets meer van te redden is?

        Een warmtecapaciteit van nul of lager (θ₃ ≤ 0) kan fysisch niet, en de
        RLS komt daar met een vergeetfactor van 0,998 pas na weken uit, als hij
        er al uitkomt. Gezien op Tweakers (21-11-2026): C = −674 Wh/K na 132
        uur, samen met U = 700 W/K en g = 25,7, op een hybride waarvan het
        model de bijspringende ketel niet zag. Zo'n model begint opnieuw vanaf
        de warmteverliesanalyse (zie QuattMpcSensor._reset_broken_model).

        Andere waarden buiten bereik (U, g, P_c) tellen hier niet: daar kan RLS
        uit terugkomen, en ``is_converged`` houdt het model intussen buiten
        gebruik.
        """
        # NaN in θ (een sensor die "nan" doorgaf, vóór v0.10.7-beta.7) is net
        # zo onherstelbaar: elke volgende update rekent NaN door.
        theta = self._rls.theta
        return bool(not np.all(np.isfinite(theta)) or theta[2] <= 0)

    @property
    def is_converged(self) -> bool:
        return bool(
            self._rls.n_updates >= RLS_MIN_UPDATES
            and self._n_updates_min() >= RLS_MIN_UPDATES
            and self._params_sane()
        )

    def _params_sane(self) -> bool:
        """Check if learned parameters are within physically plausible bounds."""
        p = self.raw_params
        if p is None:
            return False
        return bool(
            U_MIN <= p["U"] <= U_MAX
            and C_MIN <= p["C"] <= C_MAX
            and G_MIN <= p["g"] <= G_MAX
            and P_C_MIN <= p["P_c"] <= P_C_MAX
        )

    @property
    def raw_params(self) -> dict | None:
        """Extract physical parameters from θ. Returns None if θ₃ ≈ 0."""
        theta = self._rls.theta
        if abs(theta[2]) < 1e-12:
            return None
        C = 1.0 / theta[2]
        U = theta[0] * C
        g = theta[1] * C
        anchor = getattr(self, "_p_c_anchor", None)
        p_c = anchor if anchor is not None else theta[_P_C] * C
        return {"U": U, "C": C, "g": g, "P_c": p_c}

    @property
    def params(self) -> dict:
        """Return learned parameters as a user-friendly dict."""
        p = self.raw_params
        if p is None:
            return {
                "converged": False,
                "n_updates": self._rls.n_updates,
            }
        U, C, g = p["U"], p["C"], p["g"]
        tau = C / U if U > 0 else None
        return {
            "U_wk": round(U, 1),
            "C_whk": round(C, 0),
            "g_solar": round(g, 3),
            "constant_gain_w": round(p["P_c"]),
            "constant_gain_seeded": self._p_c_seeded,
            # "warmteverliesanalyse" = vastgezet (set_p_c_anchor), "geleerd" = RLS.
            "constant_gain_source": (
                "warmteverliesanalyse"
                if getattr(self, "_p_c_anchor", None) is not None
                else "geleerd"
            ),
            "tau_hours": round(tau, 1) if tau else None,
            "n_updates": self._rls.n_updates,
            # Convergentie kijkt naar de traagste parameter; n_updates alleen
            # loopt ook door in uren waarin C bevroren is. Een vastgezette
            # P_c telt niet mee (zie _n_updates_min).
            "n_updates_min": self._n_updates_min(),
            "converged": self.is_converged,
            "u_prior_wk": round(self._u_prior, 1) if self._u_prior else None,
            "u_anchored": self._u_anchored,
        }

    def to_dict(self) -> dict:
        """Serialise full state for persistence."""
        return {
            "rls": self._rls.to_dict(),
            "u_prior": self._u_prior,
            "p_c_seeded": self._p_c_seeded,
            "p_c_anchor": getattr(self, "_p_c_anchor", None),
            "prev_t_indoor": self._prev_t_indoor,
            "prev_t_outdoor": self._prev_t_outdoor,
            "prev_q_hp": self._prev_q_hp,
            "prev_q_solar": self._prev_q_solar,
            "prev_timestamp": (
                self._prev_timestamp.isoformat()
                if self._prev_timestamp
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> OnlineRCModel:
        """Deserialise from stored state."""
        model = cls.__new__(cls)
        model._rls = _with_constant_gain(RLSEstimator.from_dict(data["rls"]))
        model._u_prior = data.get("u_prior")
        model._p_c_seeded = bool(data.get("p_c_seeded", False))
        model._p_c_anchor = data.get("p_c_anchor")
        # Niet gepersisteerd: dit beschrijft de laatste update, niet de staat.
        # Wel expliciet zetten — from_dict gebruikt cls.__new__ en slaat
        # __init__ dus over, waardoor elk veld dat hier ontbreekt later een
        # AttributeError geeft in plaats van een default.
        model._u_anchored = False
        model._prev_t_indoor = data.get("prev_t_indoor")
        model._prev_t_outdoor = data.get("prev_t_outdoor")
        model._prev_q_hp = data.get("prev_q_hp")
        model._prev_q_solar = data.get("prev_q_solar")
        ts = data.get("prev_timestamp")
        model._prev_timestamp = (
            datetime.fromisoformat(ts) if ts else None
        )
        return model


def _with_constant_gain(rls: RLSEstimator) -> RLSEstimator:
    """Breid een opgeslagen 1R1C zonder P_c uit naar vier parameters.

    θ₁..θ₃ en hun covariantie blijven staan, zodat een draaiende installatie
    niet opnieuw hoeft te leren. θ₄ begint op nul, dus het model rekent
    precies als voorheen tot ``seed_constant_gain`` een startwaarde geeft.

    De teller van θ₄ neemt die van θ₃ over. Anders zou ``is_converged`` het
    model na de update dagenlang afkeuren, en vallen de MPC en de
    uitlooptijd terug op het batchmodel. Dat is erger dan een P_c die nog
    moet bijleren vanaf een gezaaide startwaarde.
    """
    if rls.n_params >= N_PARAMS:
        return rls
    theta = np.append(rls.theta, 0.0)
    P = np.zeros((N_PARAMS, N_PARAMS))
    P[:3, :3] = rls.P
    P[_P_C, _P_C] = rls.P[2, 2] * P_C_COV_SCALE_W ** 2
    est = RLSEstimator(
        n_params=N_PARAMS,
        forgetting=rls.forgetting,
        theta=theta,
        P=P,
        n_updates=rls.n_updates,
        n_updates_per_param=np.append(
            rls.n_updates_per_param, rls.n_updates_per_param[2]
        ),
    )
    _LOGGER.info("RC model uitgebreid met constante warmtewinst (θ₄)")
    return est


def stored_heat_kwh(
    c_whk: float | None,
    t_indoor: float | None,
    reference_temp: float,
) -> float | None:
    """Warmte die in de bouwmassa zit boven ``reference_temp``, in kWh.

    E = C × ΔT, met C de geleerde warmtecapaciteit van het huis (Wh/K).

    Let op wat dit *niet* is: warmte die je eruit kunt halen en ergens anders in
    kunt stoppen. Het is wat het huis moet verliezen voordat het van de huidige
    binnentemperatuur naar de referentie zakt — de buffer waarop je kunt teren
    met de warmtepomp uit. Hoe snel dat gaat hangt van het weer af; dáár gaat
    ``simulate_coast_time`` over.

    Onder de referentie wordt het getal negatief. Dat blijft zo staan in plaats
    van afgekapt op nul: een tekort is informatie, en op nul afkappen zou een
    huis dat 1 K te koud staat niet onderscheiden van een huis dat precies goed
    staat.
    """
    if c_whk is None or c_whk <= 0 or t_indoor is None:
        return None
    return round(c_whk * (t_indoor - reference_temp) / 1000.0, 1)


def simulate_forward(
    model: OnlineRCModel,
    t_indoor_now: float,
    t_return: float,
    flow_lph: float,
    forecast_t_outdoor: list[float],
    forecast_q_solar: list[float],
    t_setpoint: float = DEFAULT_ROOM_SETPOINT,
    supply_temp_min: float = 20.0,
    supply_temp_max: float = 55.0,
    max_hours: int | None = None,
    comfort_floor: float = 19.0,
) -> list[dict]:
    """Simuleer vooruit en bereken per uur het benodigde WP-vermogen.

    De horizon volgt de aangeleverde forecast-arrays; ``max_hours`` kapt hem
    desgewenst korter af. Stond hier eerder als een hard geklemde 6 — daardoor
    bleef de simulatie op zes uur staan ook als de forecast langer was, en had
    het verhogen van ``MPC_FORECAST_HOURS`` alleen langere temperatuurrijen
    opgeleverd zonder dat de binnentemp-voorspelling meeliep.

    Returns a list of dicts with keys:
        hour, q_hp_needed_w, t_indoor_predicted, supply_temp, hp_needed,
        stored_heat_kwh
    """
    SPECIFIC_HEAT = 1.16  # Wh/(L·K)
    results: list[dict] = []
    t_in = t_indoor_now
    # Eén keer ophalen: de capaciteit verandert niet gedurende de simulatie.
    raw = model.raw_params
    c_whk = raw["C"] if raw else None

    n_hours = min(len(forecast_t_outdoor), len(forecast_q_solar))
    if max_hours is not None:
        n_hours = min(n_hours, max_hours)
    for i in range(n_hours):
        t_out = forecast_t_outdoor[i]
        q_solar = forecast_q_solar[i]

        q_hp_needed = model.calc_required_power(
            t_in, t_out, q_solar, t_setpoint
        )
        q_hp_no_solar = model.calc_required_power(
            t_in, t_out, 0.0, t_setpoint
        )

        # Calculate supply temp from required power
        # Use max(t_return, t_in) because when HP is off, the return temp
        # sensor reads low (stagnant water); during operation it would be
        # at least close to indoor temperature.
        effective_return = max(t_return, t_in)
        supply_temp = None
        if q_hp_needed > 0 and flow_lph > 0:
            delta_t = q_hp_needed / (SPECIFIC_HEAT * flow_lph)
            raw_supply = effective_return + delta_t
            supply_temp = round(
                max(supply_temp_min, min(supply_temp_max, raw_supply)), 1
            )

        supply_temp_no_solar = None
        if q_hp_no_solar > 0 and flow_lph > 0:
            delta_t_ns = q_hp_no_solar / (SPECIFIC_HEAT * flow_lph)
            raw_supply_ns = effective_return + delta_t_ns
            supply_temp_no_solar = round(
                max(supply_temp_min, min(supply_temp_max, raw_supply_ns)), 1
            )

        # Predict indoor temp with the calculated heating
        t_in_next = model.predict_t_indoor(t_in, t_out, q_hp_needed, q_solar)

        results.append({
            "hour": i,
            "q_hp_needed_w": round(q_hp_needed),
            "t_indoor_predicted": round(t_in_next, 1),
            "supply_temp": supply_temp,
            "supply_temp_no_solar": supply_temp_no_solar,
            "hp_needed": bool(q_hp_needed > 200),  # MIN_HEATING_WATTS
            # De buffer hoort bij de voorspelde temperatuur van dít uur, dus
            # bij t_in_next — niet bij de stand waarmee het uur begon.
            "stored_heat_kwh": stored_heat_kwh(c_whk, t_in_next, comfort_floor),
        })

        t_in = t_in_next

    return results


@dataclass(frozen=True)
class AdviceDemand:
    """Het vermogen achter het aanvoeradvies, uitgesplitst (zie ``advice_demand``)."""

    stationary_w: float
    catchup_w: float
    total_w: float
    capped_w: float
    capacity_w: float | None

    @property
    def capped(self) -> bool:
        # bool(): de capaciteit komt uit een numpy-fit, en dan is de vergelijking
        # een numpy.bool, die Home Assistant niet naar JSON krijgt (beta.8/9:
        # de hele MPC-sensor gaf 500).
        return bool(self.capped_w < self.total_w)


def cap_demand(
    total_w: float,
    stationary_w: float,
    capacity_w: float | None,
    catchup_w_per_k: float,
) -> float:
    """De vraag begrensd op de capaciteit, maar nooit onder het warmteverlies
    plus de inhaalterm van de eerste ``CAPACITY_FREE_CATCHUP_K`` tekort.

    ``catchup_w_per_k`` is de inhaalterm per kelvin (inhaalfactor · U).

    De capaciteitsgrens is een vangrail tegen een grote inhaalvraag, en mag
    alleen de inhaalterm afkappen. Onder het warmteverlies garandeert een
    advies dat de kamer afkoelt, en kan de pomp het verlies echt niet halen,
    dan kost een hoger advies niets: hij levert wat hij kan.

    Gevonden op Tweakers (24-11-2026): de geleerde vriescurve gaf bij −2,8 °C
    hooguit ~2,5 kW tegen 4,2 kW warmteverlies. Het advies zakte tien uur lang
    van 31 naar 26 °C terwijl het kouder werd, en de kamer van 19,6 naar 18,6.
    De vriescurve is het meeste dat de pomp per uur ooit leverde, niet wat hij
    kan: ontdooiingen, stille modus en het eigen plafond drukken dat getal.

    Tweakers 25-11-2026 (Duo): met de grens op het warmteverlies viel bij een
    vriescurve onder het verlies de hele inhaalterm weg. Het advies werd het
    buitentemperatuuradvies, het plafond hield de pomp op ~31 °C en halve
    kracht, en de kamer zakte. Omdat het advies begrensd was, leerde
    ``BalanceBias`` niet, en de pomp kon nooit laten zien dat hij meer kan.
    De eerste kelvin tekort is daarom altijd vrij: een klein tekort is geen
    adviespiek, en kan de pomp het niet, dan kost het niets. Doorlopend in
    capaciteit en tekort, dus geen sprong als de curve het verlies kruist.
    """
    if capacity_w is None:
        return total_w
    free_w = stationary_w + catchup_w_per_k * CAPACITY_FREE_CATCHUP_K
    return min(total_w, max(capacity_w, free_w))


class BalanceBias:
    """Verschuiving van het balanspunt die een blijvend tekort wegwerkt.

    Het I-deel naast de inhaalterm (het P-deel): per tik
    ``bias += factor · tekort · dt / Ti``, in kelvin balanspunt. Het advies
    rekent dan met ``U · (T_balans + bias − T_buiten)``. Zie
    ``BALANCE_BIAS_TI_HOURS`` voor de waarden en waarom.

    Leert alleen als het advies stuurt, met stookvraag, niet begrensd, met een
    tekort binnen ``max_deficit_k`` en een setpoint dat ``settle_hours`` gelijk
    is gebleven. Begrensd op ``±max_k``.

    Het setpoint en sinds wanneer het zo staat horen bij de bewaarde toestand
    (``state``). Zonder die twee telde de eerste tik na elke herstart als een
    setpointwissel, en leerde de correctie na elke update drie uur niets.
    Alleen zonder bewaard setpoint (eerste start, of opslag van vóór deze
    velden) wacht hij één keer: dan is niet bekend of er net gewisseld is.
    """

    def __init__(
        self,
        ti_hours: float,
        max_k: float,
        settle_hours: float,
        max_deficit_k: float,
        max_gap_s: float,
        catchup_factor: float,
        bias_k: float = 0.0,
        setpoint: float | None = None,
        setpoint_since: float | None = None,
    ) -> None:
        self.ti_hours = ti_hours
        self.max_k = max_k
        self.settle_s = settle_hours * 3600.0
        self.max_deficit_k = max_deficit_k
        self.max_gap_s = max_gap_s
        self.catchup_factor = catchup_factor
        self.bias_k = max(-max_k, min(max_k, bias_k))
        # Alleen samen geldig: een setpoint zonder tijdstip zegt niet of het
        # al lang genoeg staat.
        if setpoint is not None and setpoint_since is not None:
            self._setpoint: float | None = setpoint
            self._setpoint_since: float | None = setpoint_since
        else:
            self._setpoint, self._setpoint_since = None, None
        self._last_ts: float | None = None

    def state(self) -> dict[str, float | None]:
        """Wat over een herstart heen moet: de bias en het setpoint met zijn tijdstip."""
        return {
            "bias_k": round(self.bias_k, 4),
            "setpoint": self._setpoint,
            "setpoint_since": self._setpoint_since,
        }

    def update(
        self,
        ts: float,
        t_setpoint: float | None,
        t_indoor: float | None,
        stationary_w: float,
        capped: bool,
        steering: bool,
    ) -> bool:
        """Eén tik. True als de bias is bijgewerkt."""
        if t_setpoint is not None and (
            self._setpoint is None or abs(t_setpoint - self._setpoint) > 0.05
        ):
            self._setpoint, self._setpoint_since = t_setpoint, ts
        prev, self._last_ts = self._last_ts, ts
        if prev is None or ts <= prev or ts - prev > self.max_gap_s:
            return False
        if not steering or capped or stationary_w <= 0:
            return False
        if t_setpoint is None or t_indoor is None or self._setpoint_since is None:
            return False
        if ts - self._setpoint_since < self.settle_s:
            return False
        deficit = t_setpoint - t_indoor
        if abs(deficit) > self.max_deficit_k:
            return False
        step = self.catchup_factor * deficit * (ts - prev) / 3600.0 / self.ti_hours
        self.bias_k = max(-self.max_k, min(self.max_k, self.bias_k + step))
        return True


def advice_demand(
    u_wk: float,
    balance_point: float,
    t_outdoor: float,
    t_indoor: float | None,
    t_setpoint: float,
    catchup_factor: float,
    capacity_w: float | None,
) -> AdviceDemand:
    """Het vermogen waar het aanvoeradvies op rekent, voor één moment.

    ``U · max(0, T_balans − T_buiten)`` plus ``catchup_factor · U · (setpoint −
    kamer)``, begrensd op ``capacity_w`` maar niet onder het warmteverlies plus
    de eerste kelvin inhaalterm (zie ``cap_demand``). Boven het balanspunt geen inhaalterm.
    Zie ``QuattMpcSensor._advice_demand`` voor het waarom van elk deel.

    Eén functie voor het advies van nu én elk uur van de vooruitblik, zodat de
    twee niet uit elkaar kunnen lopen. Tot v0.10.6-beta.8 rekende de vooruitblik
    nog met ``calc_required_power``: het hele tekort in één uur, onbegrensd. Een
    gebruiker met C ≈ 21 kWh/K zag daardoor 21 kW en 52,9 °C bij +0, naast een
    advies van 36,8 °C.
    """
    stationary = u_wk * max(0.0, balance_point - t_outdoor)
    catchup = 0.0
    if t_indoor is not None and stationary > 0:
        catchup = catchup_factor * u_wk * (t_setpoint - t_indoor)
    total = stationary + catchup
    capped = cap_demand(total, stationary, capacity_w, catchup_factor * u_wk)
    return AdviceDemand(
        stationary_w=stationary,
        catchup_w=catchup,
        total_w=total,
        capped_w=capped,
        capacity_w=capacity_w,
    )


def rc_advice_demand(
    model: OnlineRCModel,
    t_outdoor: float,
    t_indoor: float | None,
    t_setpoint: float,
    q_solar_wm2: float | None,
    catchup_factor: float,
    capacity_w: float | None,
) -> AdviceDemand | None:
    """Het vermogen volgens het lerende huismodel (1R1C), voor één moment.

    De stationaire vraag is wat het 1R1C zelf zegt dat er nodig is om de kamer
    op het setpoint te houden (dT/dt = 0 bij T_binnen = setpoint):
    ``U · (setpoint − T_buiten) − g · Q_zon − P_c``, niet onder nul. Daarbij
    dezelfde inhaalterm en capaciteitsgrens als ``advice_demand``, zodat het
    verschil tussen de bronnen alleen in de basis zit: geleerde U, de zon van
    nu, de constante warmtewinst, en een setpoint dat 's nachts lager mag zijn.

    Waarom deze bron bestaat: dat het 1R1C slechter stuurt dan de rechte lijn
    is in november 2026 alleen op één huis vastgesteld, met een 1R1C waarvan P_c
    nog vrij meegroeide met C (sinds v0.10.7-beta.2 vastgezet). Als kiesbare
    bron, gemeten met het sturingseffect, toetst het zich op meer huizen.

    Een mogelijke valkuil, nog niet gemeten: P_c komt uit het balanspunt, en
    dat is op daggemiddelden gefit en bevat de gemiddelde zon al. De zonterm
    erbovenop kan de zon deels dubbel tellen. Bij de rechte lijn maakte dat de
    zonuren 300 W slechter (zie ``QuattMpcSensor._advice_demand``).

    None als het model geen parameters heeft. Of het model betrouwbaar is
    (``is_converged``), beslist de aanroeper.
    """
    p = model.raw_params
    if p is None:
        return None
    u_wk = float(p["U"])
    stationary = max(
        0.0,
        u_wk * (t_setpoint - t_outdoor)
        - float(p["g"]) * (q_solar_wm2 or 0.0)
        - float(p["P_c"]),
    )
    catchup = 0.0
    if t_indoor is not None and stationary > 0:
        catchup = catchup_factor * u_wk * (t_setpoint - t_indoor)
    total = stationary + catchup
    capped = cap_demand(total, stationary, capacity_w, catchup_factor * u_wk)
    return AdviceDemand(
        stationary_w=stationary,
        catchup_w=catchup,
        total_w=total,
        capped_w=capped,
        capacity_w=capacity_w,
    )


def supply_temp_for(
    q_w: float,
    t_return: float | None,
    flow_lph: float,
    supply_temp_min: float,
    supply_temp_max: float,
    emitter=None,
    t_room: float | None = None,
) -> float:
    """Aanvoertemperatuur die ``q_w`` levert bij dit debiet, geklemd.

    Met ``emitter`` en ``t_room`` volgens het afgiftemodel
    (``analysis.emitter``), anders uit de retour. Wie de retour van nu heeft
    met een draaiende pomp, laat ``emitter`` weg: die retour is voor dit
    moment nauwkeuriger (0,09 tegen 0,40 K gemiddelde afwijking, november 2026).
    """
    if emitter is not None and t_room is not None:
        t_supply = emitter.supply(q_w, t_room, flow_lph)
    elif t_return is not None:
        t_supply = t_return + q_w / (1.16 * flow_lph)
    else:
        raise ValueError("geen retour en geen afgiftemodel")
    return round(max(supply_temp_min, min(supply_temp_max, t_supply)), 1)


def simulate_advice_forward(
    model: OnlineRCModel,
    t_indoor_now: float,
    t_return: float,
    flow_lph: float,
    forecast_t_outdoor: list[float],
    forecast_q_solar: list[float],
    *,
    u_wk: float,
    balance_point: float,
    t_setpoint: float,
    catchup_factor: float,
    capacity_w: Callable[[float], float | None],
    supply_temp_min: float = 20.0,
    supply_temp_max: float = 55.0,
    max_hours: int | None = None,
    comfort_floor: float = 19.0,
    emitter=None,
    pump_running: bool = True,
) -> list[dict]:
    """De vooruitblik: per uur het advies, met het RC-model als kamer.

    Met ``emitter`` (afgiftemodel) rekent elk uur na uur 0 de aanvoer uit het
    afgiftemodel met de voorspelde kamer, in plaats van met de retour van nu;
    uur 0 ook, als de pomp stilstaat. De retour van vier uur eerder zat op
    het gemeten huis gemiddeld 0,61 K naast, het afgiftemodel 0,40 K.

    Elk uur rekent ``advice_demand`` met de verwachte buitentemperatuur en de
    kamertemperatuur die het model voor dat uur voorspelt, begrensd op wat de
    warmtepomp bij die buitentemperatuur kan. Het model schuift de kamer
    daarna een uur op met dát vermogen en de verwachte zon. Uur 0 met de
    gemeten buitentemperatuur is zo hetzelfde getal als het advies van nu.

    Zon zit niet in de vraag (het balanspunt bevat de gemiddelde zon al, zie
    ``advice_demand``), maar wel in de kamer: een zonnig uur warmt de kamer op,
    en een kamer boven het setpoint verlaagt het volgende uur via de
    inhaalterm. ``supply_temp_no_solar`` volgt een tweede kamer zonder zon.

    Dezelfde sleutels als ``simulate_forward``, plus ``begrensd``.
    """
    results: list[dict] = []
    raw = model.raw_params
    c_whk = raw["C"] if raw else None

    def stap(t_in: float, t_out: float, hour: int) -> tuple[AdviceDemand, float, float | None]:
        d = advice_demand(
            u_wk, balance_point, t_out, t_in, t_setpoint, catchup_factor,
            capacity_w(t_out),
        )
        if d.total_w <= 0:
            return d, 0.0, None
        q = max(0.0, d.capped_w)
        use_emitter = emitter is not None and (hour > 0 or not pump_running)
        supply = (
            supply_temp_for(q, t_return, flow_lph, supply_temp_min, supply_temp_max,
                            emitter=emitter if use_emitter else None, t_room=t_in)
            if flow_lph > 0
            else None
        )
        return d, q, supply

    n_hours = min(len(forecast_t_outdoor), len(forecast_q_solar))
    if max_hours is not None:
        n_hours = min(n_hours, max_hours)
    t_in = t_in_ns = t_indoor_now
    for i in range(n_hours):
        t_out = forecast_t_outdoor[i]
        q_solar = forecast_q_solar[i]

        d, q, supply = stap(t_in, t_out, i)
        _d_ns, q_ns, supply_ns = stap(t_in_ns, t_out, i)

        t_in_next = model.predict_t_indoor(t_in, t_out, q, q_solar)
        t_in_ns = model.predict_t_indoor(t_in_ns, t_out, q_ns, 0.0)

        results.append({
            "hour": i,
            "q_hp_needed_w": round(q),
            "t_indoor_predicted": round(t_in_next, 1),
            "supply_temp": supply,
            "supply_temp_no_solar": supply_ns,
            "hp_needed": bool(q > 200),  # MIN_HEATING_WATTS
            "begrensd": bool(d.capped),
            "stored_heat_kwh": stored_heat_kwh(c_whk, t_in_next, comfort_floor),
        })
        t_in = t_in_next

    return results


def advice_forward_without_model(
    t_indoor_now: float | None,
    t_return: float,
    flow_lph: float,
    forecast_t_outdoor: list[float],
    *,
    u_wk: float,
    balance_point: float,
    t_setpoint: float,
    catchup_factor: float,
    capacity_w: Callable[[float], float | None],
    supply_temp_min: float = 20.0,
    supply_temp_max: float = 55.0,
    max_hours: int | None = None,
    emitter=None,
    pump_running: bool = True,
) -> list[dict]:
    """De vooruitblik zonder geconvergeerd huismodel: per uur het advies.

    ``emitter``: zie ``simulate_advice_forward``; de kamer is hier het setpoint.

    Dezelfde ``advice_demand`` als het advies van nu, zodat uur 0 (met de
    gemeten buitentemperatuur) hetzelfde getal is als de state. Tot
    v0.10.7-beta.4 rekende de terugval met de stooklijn, trok hij de zon af en
    kende hij geen inhaalterm en geen capaciteitsgrens, zodat +0 naast het
    advies lag.

    De inhaalterm telt alleen in uur 0. Hoe snel de kamer daarna bijtrekt hangt
    af van C, en die is er juist niet als het model niet geconvergeerd is. Vanaf
    uur 1 rekent de tabel dus alsof de kamer op het setpoint staat: een
    bewuste onderschatting zolang er een tekort is, liever dan een verzonnen C.
    Om dezelfde reden blijven ``t_indoor_predicted`` en ``stored_heat_kwh``
    leeg. Zon zit niet in de vraag (zie ``advice_demand``), en zonder kamer ook
    niet in de inhaalterm, dus ``supply_temp_no_solar`` is gelijk aan
    ``supply_temp``.

    Dezelfde sleutels als ``simulate_advice_forward``.
    """
    n_hours = len(forecast_t_outdoor)
    if max_hours is not None:
        n_hours = min(n_hours, max_hours)
    results: list[dict] = []
    for i in range(n_hours):
        t_out = forecast_t_outdoor[i]
        d = advice_demand(
            u_wk, balance_point, t_out,
            t_indoor_now if i == 0 else None,
            t_setpoint, catchup_factor, capacity_w(t_out),
        )
        q = max(0.0, d.capped_w) if d.total_w > 0 else 0.0
        use_emitter = emitter is not None and (i > 0 or not pump_running)
        supply = (
            supply_temp_for(q, t_return, flow_lph, supply_temp_min, supply_temp_max,
                            emitter=emitter if use_emitter else None,
                            t_room=(t_indoor_now if i == 0 and t_indoor_now is not None
                                    else t_setpoint))
            if d.total_w > 0 and flow_lph > 0
            else None
        )
        results.append({
            "hour": i,
            "q_hp_needed_w": round(q),
            "t_indoor_predicted": None,
            "supply_temp": supply,
            "supply_temp_no_solar": supply,
            "hp_needed": bool(q > 200),  # MIN_HEATING_WATTS
            "begrensd": bool(d.capped),
            "stored_heat_kwh": None,
        })
    return results


def simulate_coast_time(
    model: OnlineRCModel,
    t_indoor_now: float,
    comfort_floor: float,
    forecast_t_outdoor: list[float],
    forecast_q_solar: list[float],
    step_minutes: int = 15,
    max_hours: int = 12,
) -> dict:
    """Simulate the free cool-down (heat pump OFF) and report how long the
    house can coast before the indoor temperature reaches ``comfort_floor``.

    Used by external control to decide whether the heat pump may be throttled during
    an expensive tariff window while the battery covers the load — only as long
    as the house stays warm enough.

    The forecast solar gain is fed in per step, so the learned ``g·Q_solar``
    term slows (or on a sunny day even reverses) the cool-down: **predicted
    sunshine extends the coast time**. This is why the calculation runs on the
    RC model rather than on crude outdoor-temperature buckets.

    Args:
        model: the learned RC model (uses ``predict_t_indoor`` with q_hp = 0).
        t_indoor_now: current indoor temperature (°C).
        comfort_floor: lowest acceptable indoor temperature (°C).
        forecast_t_outdoor: hourly outdoor temperature forecast (°C).
        forecast_q_solar: hourly solar input forecast (W/m², Open-Meteo
            shortwave radiation — same unit the model was trained on).
        step_minutes: simulation resolution.
        max_hours: cap on how far ahead to look.

    Returns dict:
        coast_minutes: int | None — minutes until the floor is reached, capped
            at ``max_hours * 60``. ``None`` if the model is not usable.
        comfort_at_risk: bool — True if the floor is reached within max_hours.
        reaches_floor: bool — False when the house never cools to the floor
            (e.g. enough solar gain), in which case coast_minutes is the cap.
        trajectory: list[dict] — {minute, t_indoor_predicted} samples.
    """
    # Model unusable (θ₃ ≈ 0 → no thermal capacity identified yet).
    if model.raw_params is None:
        return {
            "coast_minutes": None,
            "comfort_at_risk": False,
            "reaches_floor": False,
            "trajectory": [],
        }

    # Already at/below the floor → no headroom to coast.
    if t_indoor_now <= comfort_floor:
        return {
            "coast_minutes": 0,
            "comfort_at_risk": True,
            "reaches_floor": True,
            "trajectory": [{"minute": 0, "t_indoor_predicted": round(t_indoor_now, 2)}],
        }

    dt_hours = step_minutes / 60.0
    total_steps = int(round(max_hours * 60 / step_minutes))
    t_in = t_indoor_now
    elapsed_min = 0.0
    trajectory: list[dict] = [{"minute": 0, "t_indoor_predicted": round(t_in, 2)}]

    for step in range(total_steps):
        # Hold the forecast for the hour this step falls in; persist the last
        # known value when the forecast runs out (assume conditions continue).
        hour_idx = int(elapsed_min // 60)
        t_out = _forecast_at(forecast_t_outdoor, hour_idx)
        q_solar = _forecast_at(forecast_q_solar, hour_idx)
        if t_out is None:
            break  # no outdoor forecast at all → cannot simulate

        t_next = model.predict_t_indoor(t_in, t_out, 0.0, q_solar or 0.0, dt_hours)

        if t_next <= comfort_floor:
            # Linear-interpolate within this step for a smoother minute value.
            if t_in != t_next:
                frac = (t_in - comfort_floor) / (t_in - t_next)
                frac = min(max(frac, 0.0), 1.0)
            else:
                frac = 0.0
            elapsed_min += frac * step_minutes
            trajectory.append(
                {"minute": round(elapsed_min), "t_indoor_predicted": round(comfort_floor, 2)}
            )
            return {
                "coast_minutes": int(round(elapsed_min)),
                "comfort_at_risk": True,
                "reaches_floor": True,
                "trajectory": trajectory,
            }

        t_in = t_next
        elapsed_min += step_minutes
        trajectory.append(
            {"minute": round(elapsed_min), "t_indoor_predicted": round(t_in, 2)}
        )

    # Never reached the floor within max_hours (warm enough / enough sun).
    return {
        "coast_minutes": int(round(max_hours * 60)),
        "comfort_at_risk": False,
        "reaches_floor": False,
        "trajectory": trajectory,
    }


def simulate_recovery_on_min(
    model: OnlineRCModel,
    t_start: float,
    t_target: float,
    forecast_t_outdoor: list[float],
    forecast_q_solar: list[float],
    min_power_at,
    start_minute: float = 0.0,
    step_minutes: int = 15,
    max_hours: int = 12,
) -> int | None:
    """Minuten om van ``t_start`` naar ``t_target`` te komen op het minimumvermogen.

    Het vervolg op de uitloop: het huis is tot de comfortvloer gezakt, en de
    warmtepomp gaat weer aan. Op zijn laagste stand is dat de zuinigste maar
    ook de traagste weg terug. Ligt het minimum nauwelijks boven het
    warmteverlies, dan duurt herstellen lang — zo lang dat de firmware in de
    praktijk hoger gaat draaien. Dat is precies wat dit getal moet laten zien:
    de uitloop is geleende warmte, en terugbetalen gaat niet vanzelf snel.

    ``start_minute`` is waar de uitloop eindigde; vanaf daar loopt de forecast
    verder. ``min_power_at`` geeft het minimum (W) bij een buitentemperatuur.

    Geeft ``None`` als het doel binnen ``max_hours`` niet gehaald wordt, of als
    het model of de forecast ontbreekt.
    """
    if model.raw_params is None or not forecast_t_outdoor:
        return None
    if t_start >= t_target:
        return 0
    dt_hours = step_minutes / 60.0
    t_in = t_start
    elapsed = 0.0
    for _ in range(int(round(max_hours * 60 / step_minutes))):
        hour_idx = int((start_minute + elapsed) // 60)
        t_out = _forecast_at(forecast_t_outdoor, hour_idx)
        q_solar = _forecast_at(forecast_q_solar, hour_idx) or 0.0
        q_min = min_power_at(t_out)
        if q_min is None:
            return None
        t_next = model.predict_t_indoor(t_in, t_out, q_min, q_solar, dt_hours)
        if t_next >= t_target:
            frac = (t_target - t_in) / (t_next - t_in) if t_next != t_in else 0.0
            return int(round(elapsed + min(max(frac, 0.0), 1.0) * step_minutes))
        t_in = t_next
        elapsed += step_minutes
    return None


def _forecast_at(values: list[float], idx: int) -> float | None:
    """Return ``values[idx]``, falling back to the last entry (persistence)."""
    if not values:
        return None
    if idx < len(values):
        return values[idx]
    return values[-1]
