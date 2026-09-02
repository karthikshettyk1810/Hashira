"""Systems and entities — the nouns of System IR (spec §7-§10)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .base import IRModel, SourceLocation, TechnologyInfo, utc_now
from .enums import Confidence, EntityStatus, EntityType, IdentityClaimKind, Origin
from .ids import EntityID, IDPrefix, SystemID, entity_uri, new_id, system_uri


class System(IRModel):
    """A top-level software system boundary (§8).

    What counts as one system is a judgement the operator makes, not one Hashira
    infers: a monorepo may host several, and one system may span repositories.
    """

    id: SystemID = Field(default_factory=lambda: new_id(IDPrefix.SYSTEM))
    name: str = Field(min_length=1)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str | None = None
    repositories: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def uri(self) -> str:
        return system_uri(self.id)


class IdentityClaim(IRModel):
    """One signal that an entity is the entity we think it is (§10).

    Identity resolution is a ladder, not a lookup: a stable symbol id beats a
    qualified name, which beats a file path. Each rung records what it matched on
    and how strongly, so a wrong merge can be explained and undone.
    """

    kind: IdentityClaimKind
    value: str
    origin: Origin
    confidence: Confidence
    observed_at: datetime = Field(default_factory=utc_now)


class Entity(IRModel):
    """A canonical thing in the system (§9).

    The envelope stays small on purpose. Technology-specific data lives in
    ``metadata`` or an adapter extension so that adding a framework never
    reshapes the core (§9, §31).
    """

    id: EntityID = Field(default_factory=lambda: new_id(IDPrefix.ENTITY))
    system_id: SystemID
    type: EntityType
    name: str = Field(min_length=1)
    qualified_name: str | None = Field(
        default=None,
        description="Semantic identifier, e.g. 'payments.services.PaymentService.process'. "
        "Mutable: it is evidence about the entity, not the entity's identity.",
    )
    source: SourceLocation | None = None
    technology: TechnologyInfo | None = None
    status: EntityStatus = EntityStatus.ACTIVE
    identity_claims: list[IdentityClaim] = Field(default_factory=list)
    first_seen_revision: str | None = None
    last_seen_revision: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_identity_confidence(self) -> Entity:
        """A merged identity needs at least one claim that justified the merge."""
        if self.status is EntityStatus.SUPERSEDED and not self.identity_claims:
            raise ValueError(
                "a SUPERSEDED entity must retain the identity claims that justified it (§10)"
            )
        return self

    @property
    def uri(self) -> str:
        return entity_uri(self.id)

    @property
    def display_name(self) -> str:
        """What to show a human: the qualified name when we have one."""
        return self.qualified_name or self.name

    def strongest_claim(self, kind: IdentityClaimKind) -> IdentityClaim | None:
        """The best signal of a given kind, for resolution tie-breaks."""
        claims = [claim for claim in self.identity_claims if claim.kind == kind]
        if not claims:
            return None
        return max(claims, key=lambda claim: claim.confidence.rank)
