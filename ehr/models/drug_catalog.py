"""Drug catalog behind the medication intake combobox (see PATIENT_MEDICATION_LIST_DESIGN.md section 9).

  * drug_catalog      -- one row per drug a technician can pick (a generic or brand name), with its usual route.
                         `source` says where the row came from: 'rxnorm' (loaded from NLM's RxNorm, `rxcui` set) or
                         'manual' (entered by an administrator, `rxcui` NULL).
  * drug_dosages      -- the strengths/forms offered once a drug is chosen (one drug -> many dosages).
  * drug_frequencies  -- the practice's dosing-frequency pick list, grouped by route (ophthalmic / oral / topical / any).
  * drug_catalog_load_events -- audit row for each RxNorm name load (ehr/services/rxnorm.py).

A patient's medication row keeps its own copy of the name, strength and frequency text, so history stays readable if a
catalog row is later edited or removed; the nullable drug_id / dosage_id / frequency_id on patient_medications only record
which catalog rows were picked (NULL for a custom entry). No patient data is ever stored in or sent to these tables.
Decision support for staff only. Source values are plain strings with CHECK constraints (no DB ENUMs).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ehr.models.database import Base


class DrugCatalogEntry(Base):
    __tablename__ = "drug_catalog"
    id: Mapped[int] = mapped_column(primary_key=True)
    rxcui: Mapped[Optional[str]] = mapped_column(String(20), unique=True)    # RxNorm concept id; NULL for manual entries
    name: Mapped[str] = mapped_column(String(160))
    name_lower: Mapped[str] = mapped_column(String(160))                    # for case-insensitive prefix/substring search
    tty: Mapped[Optional[str]] = mapped_column(String(10))                  # RxNorm term type: IN (ingredient) / BN (brand name)
    default_route: Mapped[Optional[str]] = mapped_column(String(40))        # e.g. 'ophthalmic', 'oral', 'topical'
    source: Mapped[str] = mapped_column(String(10), default="manual")       # rxnorm | manual
    # RxNorm rows arrive as bare names (from the display-name list) and are resolved on first use: resolved_at is set once the
    # drug's rxcui, route and dosages have been fetched (NULL = not yet tried, or the last try failed). Manual rows leave it NULL.
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    dosages: Mapped[list["DrugDosage"]] = relationship(back_populates="drug", cascade="all, delete-orphan",
                                                       passive_deletes=True, order_by="DrugDosage.label")
    __table_args__ = (CheckConstraint("source IN ('rxnorm','manual')", name="ck_drug_catalog_source"),
                      Index("ix_drug_catalog_name_lower", "name_lower"))


class DrugDosage(Base):
    __tablename__ = "drug_dosages"
    id: Mapped[int] = mapped_column(primary_key=True)
    drug_id: Mapped[int] = mapped_column(ForeignKey("drug_catalog.id", ondelete="CASCADE"))
    rxcui: Mapped[Optional[str]] = mapped_column(String(20))                # RxNorm clinical-drug concept, when from RxNorm
    label: Mapped[str] = mapped_column(String(160))                         # what the technician sees and what is stored, e.g. "0.005% ophthalmic solution"
    dose_form: Mapped[Optional[str]] = mapped_column(String(80))
    route: Mapped[Optional[str]] = mapped_column(String(40))                # overrides the drug's default route when set
    drug: Mapped["DrugCatalogEntry"] = relationship(back_populates="dosages")
    __table_args__ = (UniqueConstraint("drug_id", "label", name="uq_drug_dosage_label"),)


class DrugCatalogLoadEvent(Base):
    """One row per attempt to load drug names from RxNorm (who, when, what came of it) -- accountability for the app's only
    outbound data feed. user_id is NULL when run from the command line."""
    __tablename__ = "drug_catalog_load_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(8))                          # ok | failed
    names_in_source: Mapped[int] = mapped_column(default=0)
    names_added: Mapped[int] = mapped_column(default=0)
    rxnorm_version: Mapped[Optional[str]] = mapped_column(String(40))
    error: Mapped[Optional[str]] = mapped_column(String(255))
    loaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("status IN ('ok','failed')", name="ck_drug_load_status"),)


class DrugFrequency(Base):
    __tablename__ = "drug_frequencies"
    id: Mapped[int] = mapped_column(primary_key=True)
    route_group: Mapped[str] = mapped_column(String(12))                    # ophthalmic | oral | topical | any
    label: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("route_group IN ('ophthalmic','oral','topical','any')", name="ck_drug_frequency_group"),
                      UniqueConstraint("route_group", "label", name="uq_drug_frequency"))
