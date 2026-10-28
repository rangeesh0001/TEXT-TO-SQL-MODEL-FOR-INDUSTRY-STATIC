"""
ugly_schema_seed.py — Gap 1: Seed a realistic enterprise database with
25 tables, legacy abbreviations, and cryptic column names.

This is the database that breaks demo agents. Every column name is an
abbreviation, every table name is a 2-3 part code. The agent hallucinating
`customers.name` instead of `cust_mstr.cust_nm` returns zero rows silently.

Run:  python ugly_schema_seed.py
Creates: enterprise.db  (SQLite, ~500 KB)
"""

import random
from datetime import datetime, timedelta, date
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Date,
    ForeignKey, Boolean, text, Index
)
from sqlalchemy.orm import declarative_base, Session

Base = declarative_base()

# ---------------------------------------------------------------------------
# Lookup / reference tables (the kind nobody documents)
# ---------------------------------------------------------------------------

class RgnLkp(Base):
    __tablename__ = "rgn_lkp"
    rgn_cd = Column(String(10), primary_key=True)
    rgn_nm = Column(String(50), nullable=False)


class DivLkp(Base):
    __tablename__ = "div_lkp"
    div_cd = Column(String(10), primary_key=True)
    div_nm = Column(String(50), nullable=False)


class DeptLkp(Base):
    __tablename__ = "dept_lkp"
    dept_cd = Column(String(10), primary_key=True)
    dept_nm = Column(String(50), nullable=False)
    div_cd = Column(String(10), ForeignKey("div_lkp.div_cd"))


class TerrLkp(Base):
    __tablename__ = "terr_lkp"
    terr_cd = Column(String(10), primary_key=True)
    terr_nm = Column(String(50), nullable=False)
    rgn_cd = Column(String(10), ForeignKey("rgn_lkp.rgn_cd"))


class OrdStsLkp(Base):
    __tablename__ = "ord_sts_lkp"
    sts_cd = Column(String(10), primary_key=True)
    sts_dsc = Column(String(50), nullable=False)


class ShpMthLkp(Base):
    __tablename__ = "shp_mth_lkp"
    shp_mth_cd = Column(String(10), primary_key=True)
    shp_mth_nm = Column(String(50), nullable=False)
    est_days = Column(Integer)
    base_rate = Column(Float)


class UomLkp(Base):
    __tablename__ = "uom_lkp"
    uom_cd = Column(String(10), primary_key=True)
    uom_nm = Column(String(30), nullable=False)
    uom_typ = Column(String(20))


class PrdCatLkp(Base):
    __tablename__ = "prd_cat_lkp"
    cat_cd = Column(String(10), primary_key=True)
    cat_dsc = Column(String(60), nullable=False)
    prnt_cat_cd = Column(String(10), nullable=True)
    actv_flg = Column(Boolean, default=True)


class RtnRsnLkp(Base):
    __tablename__ = "rtn_rsn_lkp"
    rsn_cd = Column(String(10), primary_key=True)
    rsn_dsc = Column(String(80), nullable=False)


class CntryLkp(Base):
    __tablename__ = "cntry_lkp"
    ctry_cd = Column(String(3), primary_key=True)
    ctry_nm = Column(String(60), nullable=False)
    ctry_iso3 = Column(String(3))


# ---------------------------------------------------------------------------
# Core entity tables
# ---------------------------------------------------------------------------

class CustMstr(Base):
    __tablename__ = "cust_mstr"
    cust_id = Column(Integer, primary_key=True)
    cust_nm = Column(String(100), nullable=False)
    rgn_cd = Column(String(10), ForeignKey("rgn_lkp.rgn_cd"))
    tier_cd = Column(String(10))           # BRZ / SLV / GLD / PLT
    crt_dt = Column(Date)
    upd_dt = Column(Date)
    actv_flg = Column(Boolean, default=True)


class CustAddr(Base):
    __tablename__ = "cust_addr"
    addr_id = Column(Integer, primary_key=True)
    cust_id = Column(Integer, ForeignKey("cust_mstr.cust_id"))
    addr_typ = Column(String(10))          # BILL / SHIP
    addr_ln1 = Column(String(100))
    addr_ln2 = Column(String(100))
    cty = Column(String(50))
    st_cd = Column(String(5))
    zip = Column(String(10))
    ctry_cd = Column(String(3), ForeignKey("cntry_lkp.ctry_cd"))


class CustCntct(Base):
    __tablename__ = "cust_cntct"
    cntct_id = Column(Integer, primary_key=True)
    cust_id = Column(Integer, ForeignKey("cust_mstr.cust_id"))
    cntct_nm = Column(String(100))
    cntct_typ = Column(String(10))         # PRI / ALT
    ph_nbr = Column(String(20))
    eml = Column(String(100))


class EmpMstr(Base):
    __tablename__ = "emp_mstr"
    emp_id = Column(Integer, primary_key=True)
    emp_nm = Column(String(100), nullable=False)
    dept_cd = Column(String(10), ForeignKey("dept_lkp.dept_cd"))
    mgr_emp_id = Column(Integer, nullable=True)
    hire_dt = Column(Date)
    term_dt = Column(Date, nullable=True)
    actv_flg = Column(Boolean, default=True)


class SlsrMstr(Base):
    __tablename__ = "slsr_mstr"
    slsr_id = Column(Integer, primary_key=True)
    emp_id = Column(Integer, ForeignKey("emp_mstr.emp_id"))
    terr_cd = Column(String(10), ForeignKey("terr_lkp.terr_cd"))
    quota_amt = Column(Float)
    ytd_sls_amt = Column(Float, default=0.0)


class CustSlsr(Base):
    __tablename__ = "cust_slsr"
    map_id = Column(Integer, primary_key=True)
    cust_id = Column(Integer, ForeignKey("cust_mstr.cust_id"))
    slsr_id = Column(Integer, ForeignKey("slsr_mstr.slsr_id"))
    eff_dt = Column(Date)
    exp_dt = Column(Date, nullable=True)
    prim_flg = Column(Boolean, default=True)


# ---------------------------------------------------------------------------
# Product tables
# ---------------------------------------------------------------------------

class PrdMstr(Base):
    __tablename__ = "prd_mstr"
    prd_id = Column(Integer, primary_key=True)
    prd_nm = Column(String(100), nullable=False)
    prd_sku = Column(String(30), unique=True)
    cat_cd = Column(String(10), ForeignKey("prd_cat_lkp.cat_cd"))
    uom_cd = Column(String(10), ForeignKey("uom_lkp.uom_cd"))
    lst_prc = Column(Float)                # list price
    cst_prc = Column(Float)                # cost price
    actv_flg = Column(Boolean, default=True)


class WhsMstr(Base):
    __tablename__ = "whs_mstr"
    whs_id = Column(Integer, primary_key=True)
    whs_nm = Column(String(60), nullable=False)
    whs_typ = Column(String(10))           # DIST / MFG / RET
    rgn_cd = Column(String(10), ForeignKey("rgn_lkp.rgn_cd"))
    actv_flg = Column(Boolean, default=True)


class PrdInv(Base):
    __tablename__ = "prd_inv"
    inv_id = Column(Integer, primary_key=True)
    prd_id = Column(Integer, ForeignKey("prd_mstr.prd_id"))
    whs_id = Column(Integer, ForeignKey("whs_mstr.whs_id"))
    on_hnd_qty = Column(Integer, default=0)
    rsrv_qty = Column(Integer, default=0)
    rord_pt = Column(Integer)              # reorder point
    upd_dt = Column(Date)


# ---------------------------------------------------------------------------
# Order tables (the ones devs always get wrong)
# ---------------------------------------------------------------------------

class OrdHdr(Base):
    __tablename__ = "ord_hdr"
    ord_id = Column(Integer, primary_key=True)
    cust_id = Column(Integer, ForeignKey("cust_mstr.cust_id"))
    slsr_id = Column(Integer, ForeignKey("slsr_mstr.slsr_id"), nullable=True)
    ord_dt = Column(Date)
    shp_dt = Column(Date, nullable=True)
    sts_cd = Column(String(10), ForeignKey("ord_sts_lkp.sts_cd"))
    shp_mth_cd = Column(String(10), ForeignKey("shp_mth_lkp.shp_mth_cd"))
    ttl_amt = Column(Float)                # line-item total BEFORE tax/shipping
    tax_amt = Column(Float, default=0.0)
    shp_amt = Column(Float, default=0.0)


class OrdDtl(Base):
    __tablename__ = "ord_dtl"
    dtl_id = Column(Integer, primary_key=True)
    ord_id = Column(Integer, ForeignKey("ord_hdr.ord_id"))
    prd_id = Column(Integer, ForeignKey("prd_mstr.prd_id"))
    qty_ord = Column(Integer)
    qty_shp = Column(Integer, default=0)
    unit_prc = Column(Float)
    ext_amt = Column(Float)                # qty_ord * unit_prc
    disc_pct = Column(Float, default=0.0)  # discount percent (0-100)


# ---------------------------------------------------------------------------
# AR tables (accounts receivable — wrong query = wrong cash position)
# ---------------------------------------------------------------------------

class ArHdr(Base):
    __tablename__ = "ar_hdr"
    ar_id = Column(Integer, primary_key=True)
    cust_id = Column(Integer, ForeignKey("cust_mstr.cust_id"))
    inv_dt = Column(Date)                  # invoice date  (NOT order date!)
    due_dt = Column(Date)
    inv_amt = Column(Float)                # original invoice amount
    bal_amt = Column(Float)                # remaining balance (goes down as paid)
    sts_cd = Column(String(10))            # OPEN / PD / OVD


class ArPmt(Base):
    __tablename__ = "ar_pmt"
    pmt_id = Column(Integer, primary_key=True)
    ar_id = Column(Integer, ForeignKey("ar_hdr.ar_id"))
    pmt_dt = Column(Date)
    pmt_amt = Column(Float)
    pmt_mth_cd = Column(String(10))        # ACH / CHK / CC / WIRE


# ---------------------------------------------------------------------------
# GL tables (general ledger — never query without knowing acct_typ)
# ---------------------------------------------------------------------------

class GlAcct(Base):
    __tablename__ = "gl_acct"
    acct_id = Column(Integer, primary_key=True)
    acct_nbr = Column(String(20), unique=True)
    acct_nm = Column(String(100))
    acct_typ = Column(String(10))          # ASSET / LIAB / EQ / REV / EXP
    dept_cd = Column(String(10), ForeignKey("dept_lkp.dept_cd"), nullable=True)
    actv_flg = Column(Boolean, default=True)


class GlJrnl(Base):
    __tablename__ = "gl_jrnl"
    jrnl_id = Column(Integer, primary_key=True)
    acct_id = Column(Integer, ForeignKey("gl_acct.acct_id"))
    jrnl_dt = Column(Date)
    db_amt = Column(Float, default=0.0)    # debit amount
    cr_amt = Column(Float, default=0.0)    # credit amount
    ref_nbr = Column(String(30))
    desc_txt = Column(String(200))


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

class RtnsHdr(Base):
    __tablename__ = "rtns_hdr"
    rtn_id = Column(Integer, primary_key=True)
    ord_id = Column(Integer, ForeignKey("ord_hdr.ord_id"))
    cust_id = Column(Integer, ForeignKey("cust_mstr.cust_id"))
    rtn_dt = Column(Date)
    rsn_cd = Column(String(10), ForeignKey("rtn_rsn_lkp.rsn_cd"))
    rtn_amt = Column(Float)


# ---------------------------------------------------------------------------
# Seeder
# ---------------------------------------------------------------------------

FIRST_NAMES = ["James","Mary","John","Patricia","Robert","Jennifer","Michael",
               "Linda","William","Barbara","David","Elizabeth","Richard","Susan"]
LAST_NAMES  = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller",
               "Davis","Rodriguez","Martinez","Lopez","Wilson","Anderson","Thomas"]

def _rdate(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def seed_enterprise(db_path: str = "enterprise.db"):
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as s:
        # Lookups
        regions = [("NE","Northeast"),("SE","Southeast"),("MW","Midwest"),
                   ("SW","Southwest"),("WE","West"),("NW","Northwest")]
        s.add_all([RgnLkp(rgn_cd=cd, rgn_nm=nm) for cd, nm in regions])

        divs = [("COMM","Commercial"),("ENT","Enterprise"),("SMB","Small Business")]
        s.add_all([DivLkp(div_cd=cd, div_nm=nm) for cd, nm in divs])

        depts = [("SALES","Sales","COMM"),("MKTG","Marketing","COMM"),
                 ("FIN","Finance","ENT"),("OPS","Operations","ENT"),
                 ("IT","Information Technology","ENT")]
        s.add_all([DeptLkp(dept_cd=cd, dept_nm=nm, div_cd=div) for cd,nm,div in depts])

        terrs = [("T-NE","Northeast Territory","NE"),("T-SE","Southeast Territory","SE"),
                 ("T-MW","Midwest Territory","MW"),("T-WE","West Territory","WE")]
        s.add_all([TerrLkp(terr_cd=cd, terr_nm=nm, rgn_cd=rgn) for cd,nm,rgn in terrs])

        statuses = [("OPEN","Open"),("SHIP","Shipped"),("COMP","Completed"),
                    ("CANC","Cancelled"),("PROC","Processing")]
        s.add_all([OrdStsLkp(sts_cd=cd, sts_dsc=dsc) for cd, dsc in statuses])

        shp_methods = [("STD","Standard Ground",5,8.99),("EXP","Express 2-Day",2,24.99),
                       ("ONT","Overnight",1,49.99),("FRT","Freight",10,0.0)]
        s.add_all([ShpMthLkp(shp_mth_cd=cd, shp_mth_nm=nm, est_days=d, base_rate=r)
                   for cd,nm,d,r in shp_methods])

        uoms = [("EA","Each","COUNT"),("CS","Case","COUNT"),
                ("LB","Pound","WEIGHT"),("KG","Kilogram","WEIGHT")]
        s.add_all([UomLkp(uom_cd=cd, uom_nm=nm, uom_typ=t) for cd,nm,t in uoms])

        cats = [("ELEC","Electronics",None),("CLTH","Clothing",None),
                ("FURN","Furniture",None),("BOOK","Books",None),("APPL","Appliances",None),
                ("ELEC-COMP","Computers","ELEC"),("ELEC-ACC","Accessories","ELEC")]
        s.add_all([PrdCatLkp(cat_cd=cd, cat_dsc=dsc, prnt_cat_cd=p) for cd,dsc,p in cats])

        rtn_reasons = [("DEF","Defective product"),("WRG","Wrong item received"),
                       ("NNF","No longer needed"),("DAM","Damaged in shipping")]
        s.add_all([RtnRsnLkp(rsn_cd=cd, rsn_dsc=dsc) for cd, dsc in rtn_reasons])

        countries = [("US","United States","USA"),("CA","Canada","CAN"),("GB","United Kingdom","GBR")]
        s.add_all([CntryLkp(ctry_cd=cd, ctry_nm=nm, ctry_iso3=i3) for cd,nm,i3 in countries])

        s.flush()

        # Warehouses
        warehouses = [
            WhsMstr(whs_id=1, whs_nm="East Coast DC", whs_typ="DIST", rgn_cd="NE"),
            WhsMstr(whs_id=2, whs_nm="Central Distribution", whs_typ="DIST", rgn_cd="MW"),
            WhsMstr(whs_id=3, whs_nm="West Coast Hub", whs_typ="DIST", rgn_cd="WE"),
        ]
        s.add_all(warehouses)

        # GL accounts
        gl_accounts = [
            GlAcct(acct_id=1, acct_nbr="4000-000", acct_nm="Product Revenue", acct_typ="REV", dept_cd="SALES"),
            GlAcct(acct_id=2, acct_nbr="4100-000", acct_nm="Service Revenue", acct_typ="REV", dept_cd="SALES"),
            GlAcct(acct_id=3, acct_nbr="5000-000", acct_nm="Cost of Goods Sold", acct_typ="EXP", dept_cd="OPS"),
            GlAcct(acct_id=4, acct_nbr="6000-000", acct_nm="Sales & Marketing", acct_typ="EXP", dept_cd="MKTG"),
            GlAcct(acct_id=5, acct_nbr="1000-000", acct_nm="Cash & Equivalents", acct_typ="ASSET"),
            GlAcct(acct_id=6, acct_nbr="1200-000", acct_nm="Accounts Receivable", acct_typ="ASSET"),
        ]
        s.add_all(gl_accounts)

        # Employees
        employees, emp_id = [], 1
        for dept_cd in ["SALES","SALES","SALES","MKTG","FIN","OPS","IT"]:
            nm = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            employees.append(EmpMstr(
                emp_id=emp_id, emp_nm=nm, dept_cd=dept_cd,
                hire_dt=_rdate(date(2015,1,1), date(2022,12,31)),
                actv_flg=True,
            ))
            emp_id += 1
        s.add_all(employees)

        # Salespeople (only SALES dept employees)
        salespersons = []
        terr_codes = ["T-NE","T-SE","T-MW","T-WE"]
        sales_emps = [e for e in employees if e.dept_cd == "SALES"]
        for i, emp in enumerate(sales_emps):
            salespersons.append(SlsrMstr(
                slsr_id=i+1, emp_id=emp.emp_id,
                terr_cd=terr_codes[i % len(terr_codes)],
                quota_amt=random.uniform(250_000, 750_000),
                ytd_sls_amt=0.0,
            ))
        s.add_all(salespersons)
        s.flush()

        # Products
        product_data = [
            ("Laptop Pro 15","PRD-001","ELEC-COMP","EA",1299.99,780.00),
            ("Laptop Air 13","PRD-002","ELEC-COMP","EA",999.99,600.00),
            ("Wireless Mouse","PRD-003","ELEC-ACC","EA",29.99,8.00),
            ("Mechanical Keyboard","PRD-004","ELEC-ACC","EA",149.99,45.00),
            ("4K Monitor","PRD-005","ELEC-ACC","EA",499.99,220.00),
            ("Running Shoes","PRD-006","CLTH","EA",119.99,35.00),
            ("Denim Jeans","PRD-007","CLTH","EA",59.99,18.00),
            ("Winter Jacket","PRD-008","CLTH","EA",189.99,60.00),
            ("Office Desk","PRD-009","FURN","EA",349.99,120.00),
            ("Ergonomic Chair","PRD-010","FURN","EA",499.99,180.00),
            ("Standing Desk","PRD-011","FURN","EA",599.99,210.00),
            ("Python Cookbook","PRD-012","BOOK","EA",49.99,12.00),
            ("Data Science Handbook","PRD-013","BOOK","EA",59.99,14.00),
            ("Coffee Maker","PRD-014","APPL","EA",129.99,42.00),
            ("Air Fryer XL","PRD-015","APPL","EA",99.99,30.00),
        ]
        products = []
        for i,(nm,sku,cat,uom,lst,cst) in enumerate(product_data, start=1):
            p = PrdMstr(prd_id=i, prd_nm=nm, prd_sku=sku, cat_cd=cat,
                        uom_cd=uom, lst_prc=lst, cst_prc=cst, actv_flg=True)
            products.append(p)
        s.add_all(products)

        # Inventory
        for prd in products:
            for whs in warehouses:
                s.add(PrdInv(
                    prd_id=prd.prd_id, whs_id=whs.whs_id,
                    on_hnd_qty=random.randint(10, 500),
                    rsrv_qty=random.randint(0, 20),
                    rord_pt=50,
                    upd_dt=date(2024, 6, 1),
                ))

        # Customers
        customers = []
        used_nms = set()
        rgn_cds = [r[0] for r in regions]
        tier_cds = ["BRZ","SLV","GLD","PLT"]
        tier_wts = [0.4, 0.3, 0.2, 0.1]
        for cust_id in range(1, 101):
            while True:
                nm = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
                if nm not in used_nms:
                    used_nms.add(nm)
                    break
            crd = _rdate(date(2018,1,1), date(2022,12,31))
            c = CustMstr(
                cust_id=cust_id, cust_nm=nm,
                rgn_cd=random.choice(rgn_cds),
                tier_cd=random.choices(tier_cds, weights=tier_wts, k=1)[0],
                crt_dt=crd, upd_dt=crd, actv_flg=True,
            )
            customers.append(c)
            s.add(CustAddr(addr_id=cust_id*2-1, cust_id=cust_id, addr_typ="BILL",
                           addr_ln1=f"{random.randint(100,9999)} Main St",
                           cty="Springfield", st_cd="IL", zip="62701", ctry_cd="US"))
        s.add_all(customers)

        # Customer-salesperson mapping
        for c in customers:
            slsr = random.choice(salespersons)
            s.add(CustSlsr(cust_id=c.cust_id, slsr_id=slsr.slsr_id,
                           eff_dt=date(2022,1,1), prim_flg=True))
        s.flush()

        # Orders + AR
        start_dt = date(2023, 1, 1)
        end_dt   = date(2024, 6, 30)
        ord_id, dtl_id, ar_id, pmt_id = 1, 1, 1, 1
        sts_probs = ["COMP","COMP","COMP","SHIP","PROC","CANC"]
        shp_methods_cds = ["STD","STD","EXP","ONT","FRT"]

        for _ in range(400):
            cust = random.choice(customers)
            ord_dt = _rdate(start_dt, end_dt)
            sts_cd = random.choice(sts_probs)
            shp_cd = random.choice(shp_methods_cds)
            shp_dt = ord_dt + timedelta(days=random.randint(1, 10)) if sts_cd in ("COMP","SHIP") else None
            shp_slsr = random.choice(salespersons) if random.random() > 0.2 else None

            line_items = random.sample(products, random.randint(1, 4))
            ttl = 0.0
            for prd in line_items:
                qty = random.randint(1, 8)
                disc = random.choice([0.0, 0.0, 5.0, 10.0])
                unit = prd.lst_prc * (1 - disc/100)
                ext = round(qty * unit, 2)
                ttl += ext
                s.add(OrdDtl(dtl_id=dtl_id, ord_id=ord_id, prd_id=prd.prd_id,
                             qty_ord=qty, qty_shp=qty if sts_cd in ("COMP","SHIP") else 0,
                             unit_prc=unit, ext_amt=ext, disc_pct=disc))
                dtl_id += 1

            tax = round(ttl * 0.08, 2)
            shp = 8.99 if shp_cd == "STD" else (24.99 if shp_cd == "EXP" else (49.99 if shp_cd == "ONT" else 0.0))
            s.add(OrdHdr(ord_id=ord_id, cust_id=cust.cust_id,
                         slsr_id=shp_slsr.slsr_id if shp_slsr else None,
                         ord_dt=ord_dt, shp_dt=shp_dt, sts_cd=sts_cd, shp_mth_cd=shp_cd,
                         ttl_amt=round(ttl, 2), tax_amt=tax, shp_amt=shp))

            # AR for completed orders
            if sts_cd == "COMP":
                inv_amt = round(ttl + tax + shp, 2)
                paid = random.random() > 0.15
                s.add(ArHdr(ar_id=ar_id, cust_id=cust.cust_id,
                            inv_dt=shp_dt or ord_dt,
                            due_dt=(shp_dt or ord_dt) + timedelta(days=30),
                            inv_amt=inv_amt,
                            bal_amt=0.0 if paid else inv_amt,
                            sts_cd="PD" if paid else "OPEN"))
                if paid:
                    s.add(ArPmt(pmt_id=pmt_id, ar_id=ar_id,
                                pmt_dt=(shp_dt or ord_dt) + timedelta(days=random.randint(1,28)),
                                pmt_amt=inv_amt,
                                pmt_mth_cd=random.choice(["ACH","CHK","CC","WIRE"])))
                    pmt_id += 1
                ar_id += 1

            ord_id += 1

        # GL journal entries (revenue recognition)
        jrnl_id = 1
        for _ in range(200):
            jrnl_dt = _rdate(date(2023,1,1), date(2024,6,30))
            amt = round(random.uniform(500, 50_000), 2)
            s.add(GlJrnl(jrnl_id=jrnl_id, acct_id=1, jrnl_dt=jrnl_dt,
                         db_amt=0.0, cr_amt=amt,
                         ref_nbr=f"REV-{jrnl_id:05d}", desc_txt="Product revenue recognition"))
            jrnl_id += 1
            s.add(GlJrnl(jrnl_id=jrnl_id, acct_id=3, jrnl_dt=jrnl_dt,
                         db_amt=round(amt*0.55,2), cr_amt=0.0,
                         ref_nbr=f"COGS-{jrnl_id:05d}", desc_txt="Cost of goods sold"))
            jrnl_id += 1

        # Returns
        rtn_id = 1
        for ord in s.execute(
            text("SELECT ord_id, cust_id, ord_dt FROM ord_hdr WHERE sts_cd='COMP' LIMIT 30")
        ).fetchall():
            s.add(RtnsHdr(
                rtn_id=rtn_id, ord_id=ord[0], cust_id=ord[1],
                rtn_dt=date.fromisoformat(str(ord[2])) + timedelta(days=random.randint(1,45)),
                rsn_cd=random.choice(["DEF","WRG","NNF","DAM"]),
                rtn_amt=round(random.uniform(20, 500), 2),
            ))
            rtn_id += 1

        s.commit()

    # Summary
    with Session(engine) as s:
        tables = Base.metadata.tables.keys()
        print("enterprise.db seeded successfully!")
        print(f"  Tables: {len(list(tables))}")
        for tbl in tables:
            cnt = s.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            print(f"  {tbl:<20} {cnt:>5} rows")


if __name__ == "__main__":
    seed_enterprise()
