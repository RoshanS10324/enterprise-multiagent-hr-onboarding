from pydantic import BaseModel


class VariantFlags(BaseModel):
    is_rehire: bool = False
    is_internal_transfer: bool = False
    is_agency_contractor: bool = False
    is_minor: bool = False
    is_uae_resident: bool = False
    is_remote: bool = False
