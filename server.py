from fastapi import FastAPI, HTTPException, Depends, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
import os
import qrcode
import io
from bson import ObjectId
import uuid

# Environment variables
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017/qr_menu_db")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production-2024")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# FastAPI app
app = FastAPI(title="QR Menu Pro API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB client
client = AsyncIOMotorClient(MONGO_URL)
db = client.get_database()

# Collections
categories_collection = db.categories
products_collection = db.products

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

# Create uploads directory
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ============== MODELS ==============

class CategoryCreate(BaseModel):
    name_tr: str
    name_en: str
    sort_order: int = 0
    is_active: bool = True

class CategoryUpdate(BaseModel):
    name_tr: Optional[str] = None
    name_en: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

class ProductCreate(BaseModel):
    category_id: str
    name_tr: str
    name_en: str
    description_tr: Optional[str] = None
    description_en: Optional[str] = None
    price: float
    image_url: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True

class ProductUpdate(BaseModel):
    category_id: Optional[str] = None
    name_tr: Optional[str] = None
    name_en: Optional[str] = None
    description_tr: Optional[str] = None
    description_en: Optional[str] = None
    price: Optional[float] = None
    image_url: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

class LoginRequest(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

# ============== AUTH FUNCTIONS ==============

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")

# ============== HELPER FUNCTIONS ==============

def serialize_doc(doc):
    """Convert MongoDB document to JSON serializable format"""
    if doc is None:
        return None
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    return doc

# ============== AUTH ENDPOINTS ==============

@app.post("/api/auth/login", response_model=Token)
async def login(request: LoginRequest):
    if request.username == ADMIN_USERNAME and request.password == ADMIN_PASSWORD:
        access_token = create_access_token(data={"sub": request.username})
        return {"access_token": access_token, "token_type": "bearer"}
    raise HTTPException(status_code=401, detail="Incorrect username or password")

@app.get("/api/auth/verify")
async def verify(username: str = Depends(verify_token)):
    return {"username": username}

# ============== CATEGORY ENDPOINTS ==============

@app.get("/api/categories")
async def get_categories(active_only: bool = False):
    query = {"is_active": True} if active_only else {}
    categories = await categories_collection.find(query).sort("sort_order", 1).to_list(100)
    return [serialize_doc(cat) for cat in categories]

@app.get("/api/categories/{category_id}")
async def get_category(category_id: str):
    category = await categories_collection.find_one({"_id": ObjectId(category_id)})
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    return serialize_doc(category)

@app.post("/api/categories")
async def create_category(category: CategoryCreate, username: str = Depends(verify_token)):
    category_dict = category.dict()
    category_dict["created_at"] = datetime.utcnow()
    result = await categories_collection.insert_one(category_dict)
    new_category = await categories_collection.find_one({"_id": result.inserted_id})
    return serialize_doc(new_category)

@app.put("/api/categories/{category_id}")
async def update_category(category_id: str, category: CategoryUpdate, username: str = Depends(verify_token)):
    update_data = {k: v for k, v in category.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    result = await categories_collection.update_one(
        {"_id": ObjectId(category_id)},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Category not found")
    
    updated_category = await categories_collection.find_one({"_id": ObjectId(category_id)})
    return serialize_doc(updated_category)

@app.delete("/api/categories/{category_id}")
async def delete_category(category_id: str, username: str = Depends(verify_token)):
    # Check if category has products
    products_count = await products_collection.count_documents({"category_id": category_id})
    if products_count > 0:
        raise HTTPException(status_code=400, detail="Cannot delete category with products")
    
    result = await categories_collection.delete_one({"_id": ObjectId(category_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Category not found")
    
    return {"message": "Category deleted successfully"}

# ============== PRODUCT ENDPOINTS ==============

@app.get("/api/products")
async def get_products(category_id: Optional[str] = None, active_only: bool = False):
    query = {}
    if category_id:
        query["category_id"] = category_id
    if active_only:
        query["is_active"] = True
    
    products = await products_collection.find(query).sort("sort_order", 1).to_list(1000)
    return [serialize_doc(prod) for prod in products]

@app.get("/api/products/{product_id}")
async def get_product(product_id: str):
    product = await products_collection.find_one({"_id": ObjectId(product_id)})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return serialize_doc(product)

@app.post("/api/products")
async def create_product(product: ProductCreate, username: str = Depends(verify_token)):
    # Verify category exists
    category = await categories_collection.find_one({"_id": ObjectId(product.category_id)})
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    
    product_dict = product.dict()
    product_dict["created_at"] = datetime.utcnow()
    result = await products_collection.insert_one(product_dict)
    new_product = await products_collection.find_one({"_id": result.inserted_id})
    return serialize_doc(new_product)

@app.put("/api/products/{product_id}")
async def update_product(product_id: str, product: ProductUpdate, username: str = Depends(verify_token)):
    update_data = {k: v for k, v in product.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    # If category_id is being updated, verify it exists
    if "category_id" in update_data:
        category = await categories_collection.find_one({"_id": ObjectId(update_data["category_id"])})
        if not category:
            raise HTTPException(status_code=404, detail="Category not found")
    
    result = await products_collection.update_one(
        {"_id": ObjectId(product_id)},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    
    updated_product = await products_collection.find_one({"_id": ObjectId(product_id)})
    return serialize_doc(updated_product)

@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str, username: str = Depends(verify_token)):
    result = await products_collection.delete_one({"_id": ObjectId(product_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    
    return {"message": "Product deleted successfully"}

# ============== IMAGE UPLOAD ==============

@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...), username: str = Depends(verify_token)):
    # Validate file type
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    # Generate unique filename
    file_extension = file.filename.split(".")[-1]
    unique_filename = f"{uuid.uuid4()}.{file_extension}"
    file_path = f"uploads/{unique_filename}"
    
    # Save file
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    return {"image_url": f"/uploads/{unique_filename}"}

# ============== QR CODE GENERATION ==============

@app.get("/api/qr-code")
async def generate_qr_code(url: str = "http://localhost:3000"):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save to bytes
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    
    # Save to file
    qr_path = "uploads/qr_menu.png"
    with open(qr_path, "wb") as f:
        f.write(img_bytes.getvalue())
    
    return {"qr_code_url": f"/uploads/qr_menu.png"}

# ============== HEALTH CHECK ==============

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
