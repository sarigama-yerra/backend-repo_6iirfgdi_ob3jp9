import os
import re
from typing import Optional, List

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import create_document
from schemas import Bill, BillItem

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Shop Billing OCR API"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        # Try to import database module
        from database import db

        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"

            # Try to list collections to verify connectivity
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]  # Show first 10 collections
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"

    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    # Check environment variables
    import os
    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


# --------------- OCR & Parsing Logic ---------------

class OCRResult(BaseModel):
    raw_text: str
    name: Optional[str] = None
    mrp: Optional[float] = None
    sell_price: Optional[float] = None


def parse_price(text: str) -> OCRResult:
    """Parse product name, MRP, and sell price from OCR text."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lower_text = text.lower()

    # Helper to extract number after a keyword
    def number_after(keyword_variants: List[str]) -> Optional[float]:
        pattern = r"(?:(?:" + "|".join([re.escape(k) for k in keyword_variants]) + "))\s*[:\-]?\s*₹?\s*([0-9]{2,6}(?:\.[0-9]{1,2})?)"
        m = re.search(pattern, lower_text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                return None
        return None

    mrp = number_after(["mrp", "m.r.p", "max retail", "price mrp"]) or None

    # Common variations for selling price
    sell = number_after(["sell", "sale", "sp", "selling", "offer", "now", "our price"]) or None

    # Fallback: pick numbers from text and infer
    nums = [float(n) for n in re.findall(r"(?:rs\.?|inr|₹)?\s*([0-9]{2,6}(?:\.[0-9]{1,2})?)", lower_text)]
    candidates = sorted(set(nums))
    if mrp is None and candidates:
        # assume highest is MRP
        mrp = max(candidates)
    if sell is None and candidates:
        # assume lowest or last occurrence is sell
        sell = min(candidates) if mrp and len(candidates) > 1 else candidates[-1]

    # Try to guess a name line: a line with letters and without price keywords
    name_line = None
    for l in lines[:3]:  # early lines typically contain the item name
        l_low = l.lower()
        if any(k in l_low for k in ["mrp", "sell", "price", "rs", "inr", "₹"]):
            continue
        if re.search(r"[a-zA-Z]", l):
            name_line = l.strip()
            break

    return OCRResult(raw_text=text.strip(), name=name_line, mrp=mrp, sell_price=sell)


OCR_SPACE_URL = "https://api.ocr.space/parse/image"
OCR_SPACE_APIKEY = os.getenv("OCR_SPACE_APIKEY", "helloworld")  # demo key


@app.post("/api/extract-tag", response_model=OCRResult)
async def extract_tag(file: UploadFile = File(None), url: Optional[str] = Form(None)):
    """Extract text from an uploaded image or URL and parse pricing details.
    Provide either a file or a url.
    """
    if not file and not url:
        raise HTTPException(status_code=400, detail="Provide an image file or a URL")

    files = None
    data = {"language": "eng", "OCREngine": 2, "isOverlayRequired": False}
    headers = {"apikey": OCR_SPACE_APIKEY}

    try:
        if file:
            content = await file.read()
            files = {"file": (file.filename or "image.jpg", content)}
            resp = requests.post(OCR_SPACE_URL, data=data, files=files, headers=headers, timeout=30)
        else:
            data["url"] = url
            resp = requests.post(OCR_SPACE_URL, data=data, headers=headers, timeout=30)

        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"OCR service error: {resp.text[:120]}")

        payload = resp.json()
        if not payload.get("IsErroredOnProcessing") and payload.get("ParsedResults"):
            text = "\n".join([p.get("ParsedText", "") for p in payload["ParsedResults"]])
        else:
            raise HTTPException(status_code=400, detail=payload.get("ErrorMessage", "Unable to read text"))

        parsed = parse_price(text)
        return parsed
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:120]}")


# --------------- Billing Endpoints ---------------

@app.post("/api/bills")
def create_bill(bill: Bill):
    bill_id = create_document("bill", bill)
    return {"id": bill_id, "status": "created"}


@app.get("/api/bills")
def list_bills():
    from database import get_documents
    docs = get_documents("bill", limit=20)
    # Convert ObjectId to str safely
    for d in docs:
        if "_id" in d:
            d["_id"] = str(d["_id"])  # type: ignore
    return {"items": docs}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
