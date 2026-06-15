"""Coverage as a first-class output of a check, kept as two metrics that mean
opposite things.

**Resolution coverage** is the fraction of model output columns whose lineage the
propagator could follow against the fraction it fell blind on (a column reading a
reference qualify could not attach a source to, an unexpanded ``SELECT *``, a
macro that escaped rendering). Counting model output columns rather than every
reference in every nested scope keeps a deep CTE chain from inflating the
denominator. Blindness is a capability gap, so a configurable floor turns
sustained blindness into a finding, and the floor keys on resolution only.

**Grounding coverage** is, among resolved columns, how many a fact grounded,
reported per property. An ungrounded column is the expected case under "absence
is silence", not a defect, so grounding never trips a floor on its own. Where it
earns attention is scoped to declared intent: of the columns a contract names,
how many resolved to a checkable annotation, which tells a partial adopter
whether their declarations are actually being checked.

See ``docs/design/lineage-facts.md`` ("Coverage and degradation").
"""

from __future__ import annotations

from dataclasses import dataclass

from dblect.lineage.builder import ModelResolution


@dataclass(frozen=True, slots=True)
class ResolutionCoverage:
    """Aggregate resolution coverage across every model that built, plus the
    lowest-covered models for a message that can name where the blindness is."""

    resolved_columns: int
    blind_columns: int
    unexpanded_stars: int
    worst_models: tuple[ModelResolution, ...]

    @property
    def sites(self) -> int:
        """Every resolution site across the project: model output columns whose
        lineage was attempted plus unexpanded ``SELECT *``. The denominator of the
        resolved share."""
        return self.resolved_columns + self.blind_columns + self.unexpanded_stars

    @property
    def fraction(self) -> float | None:
        """Resolved share of sites, or ``None`` when there were none (a project of
        literal-only models has no lineage to follow, which is full coverage
        rather than blindness, so the floor skips it). An unexpanded ``SELECT *``
        is one blind site of unknown width: it lowers the share rather than being
        ignored, so a fully ``SELECT *`` model reads as fully blind."""
        return self.resolved_columns / self.sites if self.sites else None

    def below(self, floor: float) -> bool:
        """Whether resolution sits under ``floor``. A project with nothing to
        resolve is never below a floor; the floor is about blindness, and there
        is none."""
        frac = self.fraction
        return frac is not None and frac < floor

    @staticmethod
    def from_models(models: tuple[ModelResolution, ...], *, worst_n: int = 5) -> ResolutionCoverage:
        resolved = sum(m.resolved_columns for m in models)
        blind = sum(m.blind_columns for m in models)
        stars = sum(m.unexpanded_stars for m in models)
        # Worst-first by resolved share, then by absolute blindness, so the
        # message names the models a reader should look at. Models with no sites
        # have no blindness and sort last.
        ranked = sorted(
            (m for m in models if m.sites),
            key=lambda m: (
                m.resolved_columns / m.sites,
                -(m.blind_columns + m.unexpanded_stars),
            ),
        )
        return ResolutionCoverage(
            resolved_columns=resolved,
            blind_columns=blind,
            unexpanded_stars=stars,
            worst_models=tuple(ranked[:worst_n]),
        )


@dataclass(frozen=True, slots=True)
class PropertyGrounding:
    """How many of a property's resolved subjects a fact grounded."""

    property_name: str
    grounded: int
    resolved: int


@dataclass(frozen=True, slots=True)
class GroundingCoverage:
    """Per-property grounding, plus the contract-scoped slice that is the one
    grounding number worth acting on: of the columns contracts name, how many
    resolved to a checkable annotation."""

    by_property: tuple[PropertyGrounding, ...]
    contract_columns: int
    contract_columns_checkable: int
