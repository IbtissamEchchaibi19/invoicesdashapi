from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import shutil
from typing import List, Optional, Union
import pandas as pd
import uvicorn
import json
import hashlib
from fastapi.middleware.wsgi import WSGIMiddleware
from pymongo import MongoClient
import gridfs
from datetime import datetime
import io
import base64
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import your existing classes
from invoice_extractor import InvoiceExtractor
from dashboard import app as dash_app, increment_data_version

# Initialize FastAPI
app = FastAPI(title="Invoice Processing API", version="1.0.0")

# Mount the Dash app - this makes it accessible at /dash_app/
app.mount("/dash_app", WSGIMiddleware(dash_app.server))

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"]
)

# Pydantic models for request/response
class DeleteInvoiceRequest(BaseModel):
    invoice_ids: List[str]  # List of invoice IDs to delete

class DeleteInvoiceResponse(BaseModel):
    message: str
    deleted_invoices: List[dict]
    not_found_invoices: List[str]
    total_deleted: int
    remaining_records: int
    errors: Optional[List[str]] = None

# Global variables
extractor = InvoiceExtractor()

# MongoDB connection
try:
    MONGODB_URI = os.getenv("MONGODB_URI")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "invoice_db")
    COLLECTION_NAME = os.getenv("COLLECTION_NAME", "invoices")
    
    if not MONGODB_URI:
        raise ValueError("MONGODB_URI not found in environment variables")
    
    client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=30000,  # 30 seconds
    connectTimeoutMS=30000,          # 30 seconds
    socketTimeoutMS=30000,           # 30 seconds
    maxPoolSize=1                    # Limit connection pool
)
    db = client[DATABASE_NAME]
    collection = db[COLLECTION_NAME]
    fs = gridfs.GridFS(db)  # For storing PDF files
    
    print("Successfully connected to MongoDB Atlas")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    raise

def get_file_hash(file_content: bytes) -> str:
    """Generate a hash for file content to track if it's been processed"""
    hash_md5 = hashlib.md5()
    hash_md5.update(file_content)
    return hash_md5.hexdigest()

def is_file_processed(filename: str, file_hash: str) -> bool:
    """Check if a file has already been processed"""
    try:
        existing_record = collection.find_one({
            "filename": filename,
            "file_hash": file_hash
        })
        return existing_record is not None
    except Exception as e:
        print(f"Error checking if file is processed: {e}")
        return False

def save_file_to_gridfs(filename: str, file_content: bytes) -> str:
    """Save file to GridFS and return the file ID"""
    try:
        file_id = fs.put(file_content, filename=filename)
        return str(file_id)
    except Exception as e:
        print(f"Error saving file to GridFS: {e}")
        raise

def get_file_from_gridfs(file_id: str) -> bytes:
    """Get file content from GridFS"""
    try:
        from bson import ObjectId
        grid_out = fs.get(ObjectId(file_id))
        return grid_out.read()
    except Exception as e:
        print(f"Error getting file from GridFS: {e}")
        raise

def delete_file_from_gridfs(file_id: str) -> bool:
    """Delete file from GridFS"""
    try:
        from bson import ObjectId
        fs.delete(ObjectId(file_id))
        return True
    except Exception as e:
        print(f"Error deleting file from GridFS: {e}")
        return False

def save_invoice_data_to_mongodb(invoice_data: List[dict], filename: str, file_hash: str, gridfs_file_id: str):
    """Save invoice data to MongoDB"""
    try:
        # Prepare documents for insertion
        documents = []
        for data in invoice_data:
            document = {
                **data,
                "filename": filename,
                "file_hash": file_hash,
                "gridfs_file_id": gridfs_file_id,
                "created_at": datetime.utcnow(),
                "processed_at": datetime.utcnow()
            }
            documents.append(document)
        
        # Insert all documents
        result = collection.insert_many(documents)
        print(f"Inserted {len(result.inserted_ids)} records to MongoDB")
        return len(result.inserted_ids)
    except Exception as e:
        print(f"Error saving invoice data to MongoDB: {e}")
        raise

def get_invoice_records_by_ids(invoice_ids: List[str]) -> tuple:
    """
    Get invoice records from MongoDB by invoice IDs
    Returns: (found_records, not_found_ids)
    """
    try:
        found_records = []
        not_found_ids = []
        
        for invoice_id in invoice_ids:
            # Try to find by invoice_id field first
            record = collection.find_one({"invoice_id": invoice_id})
            
            # If not found, try by MongoDB _id
            if not record:
                try:
                    from bson import ObjectId
                    record = collection.find_one({"_id": ObjectId(invoice_id)})
                except:
                    pass
            
            # If still not found, try by filename
            if not record:
                record = collection.find_one({"filename": invoice_id})
            
            if record:
                # Convert ObjectId to string for JSON serialization
                record["_id"] = str(record["_id"])
                found_records.append(record)
            else:
                not_found_ids.append(invoice_id)
        
        return found_records, not_found_ids
    except Exception as e:
        print(f"Error getting invoice records: {e}")
        return [], invoice_ids

def delete_records_from_mongodb(invoice_ids: List[str]) -> int:
    """
    Delete records from MongoDB by invoice IDs
    Returns: number of deleted records
    """
    try:
        deleted_count = 0
        
        for invoice_id in invoice_ids:
            # Try different ID formats
            query_conditions = [
                {"invoice_id": invoice_id},
                {"filename": invoice_id}
            ]
            
            # Try ObjectId if it's a valid format
            try:
                from bson import ObjectId
                query_conditions.append({"_id": ObjectId(invoice_id)})
            except:
                pass
            
            # Delete using any matching condition
            for condition in query_conditions:
                result = collection.delete_many(condition)
                if result.deleted_count > 0:
                    deleted_count += result.deleted_count
                    break
        
        print(f"Deleted {deleted_count} records from MongoDB")
        return deleted_count
    except Exception as e:
        print(f"Error deleting records from MongoDB: {e}")
        raise HTTPException(status_code=500, detail=f"Error deleting from database: {str(e)}")

def get_all_invoice_data_as_dataframe() -> pd.DataFrame:
    """Get all invoice data from MongoDB as a pandas DataFrame for the dashboard"""
    try:
        # Get all records from MongoDB
        cursor = collection.find({})
        records = list(cursor)
        
        if not records:
            print("No records found in MongoDB")
            return pd.DataFrame(columns=[
                'invoice_id', 'invoice_date', 'customer_name', 'customer_id',
                'customer_location', 'customer_type', 'customer_trn', 'payment_status',
                'due_date', 'product', 'qty', 'unit_price', 'total', 'amount_excl_vat',
                'vat', 'profit', 'profit_margin', 'cost_price', 'days_to_payment'
            ])
        
        # Convert to DataFrame
        df = pd.DataFrame(records)
        
        # Convert MongoDB ObjectId to string
        df['_id'] = df['_id'].astype(str)
        
        # Convert date columns to datetime
        date_columns = ['invoice_date', 'due_date', 'created_at', 'processed_at']
        for col in date_columns:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors='coerce')
        
        # Ensure numeric columns are properly typed
        numeric_cols = ['qty', 'unit_price', 'total', 'amount_excl_vat', 'vat', 
                         'profit', 'profit_margin', 'cost_price', 'days_to_payment']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        print(f"Successfully loaded {len(df)} invoice records from MongoDB")
        return df
    except Exception as e:
        print(f"Error loading data from MongoDB: {e}")
        return pd.DataFrame()

def create_temp_csv_for_dashboard():
    """Create a temporary CSV file for the dashboard to read"""
    try:
        df = get_all_invoice_data_as_dataframe()
        if not df.empty:
            # Remove MongoDB-specific columns for CSV
            columns_to_remove = ['_id', 'file_hash', 'gridfs_file_id', 'created_at', 'processed_at']
            df_csv = df.drop(columns=[col for col in columns_to_remove if col in df.columns])
            
            # Save to temporary CSV file
            df_csv.to_csv('invoice_data.csv', index=False)
            print(f"Created temporary CSV with {len(df_csv)} records")
        else:
            # Create empty CSV with headers
            empty_df = pd.DataFrame(columns=[
                'invoice_id', 'invoice_date', 'customer_name', 'customer_id',
                'customer_location', 'customer_type', 'customer_trn', 'payment_status',
                'due_date', 'product', 'qty', 'unit_price', 'total', 'amount_excl_vat',
                'vat', 'profit', 'profit_margin', 'cost_price', 'days_to_payment'
            ])
            empty_df.to_csv('invoice_data.csv', index=False)
            print("Created empty CSV file")
    except Exception as e:
        print(f"Error creating temporary CSV: {e}")

@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup"""
    print("Starting Invoice Processing API...")
    try:
        # Test MongoDB connection
        db.command('ping')
        print("MongoDB connection successful")
        
        # Create temporary CSV for dashboard
        create_temp_csv_for_dashboard()
        print("Temporary CSV created for dashboard")
    except Exception as e:
        print(f"Startup error: {e}")

@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "message": "Invoice Processing API with MongoDB",
        "endpoints": {
            "upload_invoices": "/upload-invoices/",
            "delete_invoices": "/delete-invoices/",
            "list_invoices": "/invoices/",
            "dashboard": "/dash_app/",
            "health": "/health/"
        }
    }

@app.get("/dashboard/")
async def get_dashboard():
    """Redirect directly to the dashboard"""
    return RedirectResponse(url="/dash_app/")

@app.delete("/delete-invoices/")
async def delete_invoices(request: DeleteInvoiceRequest):
    """
    Delete one or multiple invoices by their IDs
    This will remove both the PDF files from GridFS and their records from MongoDB
    """
    if not request.invoice_ids:
        raise HTTPException(status_code=400, detail="No invoice IDs provided")
    
    try:
        # Get invoice records that match the IDs
        found_records, not_found_ids = get_invoice_records_by_ids(request.invoice_ids)
        
        if not found_records:
            raise HTTPException(
                status_code=404, 
                detail=f"No invoices found with IDs: {', '.join(request.invoice_ids)}"
            )
        
        deleted_invoices = []
        errors = []
        
        # Delete PDF files from GridFS and records from MongoDB
        for record in found_records:
            try:
                # Delete PDF file from GridFS if it exists
                if 'gridfs_file_id' in record and record['gridfs_file_id']:
                    delete_file_from_gridfs(record['gridfs_file_id'])
                
                deleted_invoices.append({
                    "invoice_id": str(record.get('invoice_id', record.get('_id', 'unknown'))),
                    "filename": record.get('filename', 'unknown'),
                    "deleted_from_database": True,
                    "deleted_pdf_file": True
                })
            except Exception as e:
                errors.append(f"Error deleting files for {record.get('filename', 'unknown')}: {str(e)}")
        
        # Delete records from MongoDB
        try:
            deleted_count = delete_records_from_mongodb(request.invoice_ids)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error deleting from database: {str(e)}")
        
        # Update temporary CSV and increment data version for dashboard refresh
        create_temp_csv_for_dashboard()
        increment_data_version()
        
        # Get remaining record count
        remaining_records = collection.count_documents({})
        
        return DeleteInvoiceResponse(
            message=f"Successfully deleted {len(deleted_invoices)} invoice(s)",
            deleted_invoices=deleted_invoices,
            not_found_invoices=not_found_ids,
            total_deleted=len(deleted_invoices),
            remaining_records=remaining_records,
            errors=errors if errors else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting invoices: {str(e)}")

@app.get("/invoices/")
async def list_invoices():
    """
    List all invoices with their IDs for reference
    This helps users know which IDs they can delete
    """
    try:
        cursor = collection.find({}, {
            "_id": 1, 
            "invoice_id": 1, 
            "filename": 1, 
            "invoice_number": 1, 
            "invoice_date": 1, 
            "total": 1, 
            "customer_name": 1,
            "created_at": 1
        })
        
        invoices = []
        for record in cursor:
            invoice_info = {
                "id": str(record.get('_id')),
                "invoice_id": record.get('invoice_id', 'N/A'),
                "filename": record.get('filename', 'unknown'),
                "created_at": record.get('created_at', 'N/A')
            }
            
            # Add other relevant fields if they exist
            for field in ['invoice_number', 'invoice_date', 'total', 'customer_name']:
                if field in record and record[field] is not None:
                    invoice_info[field] = record[field]
            
            invoices.append(invoice_info)
        
        return {
            "message": f"Found {len(invoices)} invoice(s)",
            "invoices": invoices,
            "total_count": len(invoices)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing invoices: {str(e)}")

@app.post("/upload-invoices/")
async def upload_invoices(files: Union[List[UploadFile], UploadFile] = File(...)):
    """
    Upload and process invoice PDFs (supports both single and multiple files)
    Files are stored in GridFS and data is stored in MongoDB
    """
    # Convert single file to list for uniform processing
    if isinstance(files, UploadFile):
        files = [files]
    
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    
    processed_files = []
    total_new_records = 0
    errors = []
    skipped_files = []
    
    for file in files:
        if not file.filename.endswith('.pdf'):
            errors.append(f"{file.filename}: Only PDF files are allowed")
            continue
        
        try:
            # Read file content
            file_content = await file.read()
            file_hash = get_file_hash(file_content)
            
            # Check if this file has already been processed
            if is_file_processed(file.filename, file_hash):
                skipped_files.append({
                    "filename": file.filename,
                    "reason": "Already processed (no changes detected)"
                })
                continue
            
            # Save file to GridFS
            gridfs_file_id = save_file_to_gridfs(file.filename, file_content)
            
            # Create temporary file for processing
            temp_file_path = f"/tmp/{file.filename}"
            with open(temp_file_path, "wb") as temp_file:
                temp_file.write(file_content)
            
            # Process the invoice
            print(f"Processing new/changed file: {file.filename}")
            invoice_data = extractor.process_invoice(temp_file_path)
            
            # Clean up temporary file
            os.remove(temp_file_path)
            
            if invoice_data:
                # Save to MongoDB
                records_added = save_invoice_data_to_mongodb(
                    invoice_data, file.filename, file_hash, gridfs_file_id
                )
                
                processed_files.append({
                    "filename": file.filename,
                    "records_added": records_added
                })
                total_new_records += records_added
                print(f"Successfully processed {file.filename}: {records_added} records")
            else:
                errors.append(f"{file.filename}: No data extracted")
                # Delete the file from GridFS if no data was extracted
                delete_file_from_gridfs(gridfs_file_id)
                
        except Exception as e:
            error_msg = f"{file.filename}: {str(e)}"
            errors.append(error_msg)
            print(f"Error processing {file.filename}: {e}")
    
    # Update temporary CSV and increment data version for dashboard refresh
    if total_new_records > 0:
        create_temp_csv_for_dashboard()
        increment_data_version()
    
    # Get current total records
    total_records = collection.count_documents({})
    
    return {
        "message": f"Upload completed. Processed {len(processed_files)} new/changed files",
        "processed_files": processed_files,
        "skipped_files": skipped_files,
        "total_new_records": total_new_records,
        "total_records": total_records,
        "errors": errors if errors else None
    }

@app.get("/health/")
async def health_check():
    """Health check endpoint"""
    try:
        # Test MongoDB connection
        db.command('ping')
        mongodb_status = "connected"
        
        # Get record counts
        total_records = collection.count_documents({})
        unique_files = len(collection.distinct("filename"))
        
        # Get GridFS file count
        gridfs_files = fs.find().count()
        
        return {
            "status": "healthy",
            "message": "Invoice Processing API is running",
            "mongodb_status": mongodb_status,
            "dashboard_status": "mounted at /dash_app/",
            "total_records": total_records,
            "unique_files": unique_files,
            "gridfs_files": gridfs_files,
            "database_name": DATABASE_NAME,
            "collection_name": COLLECTION_NAME
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Health check failed: {str(e)}"
        }

@app.get("/refresh-dashboard/")
async def refresh_dashboard():
    """Manually refresh the dashboard data"""
    try:
        create_temp_csv_for_dashboard()
        increment_data_version()
        return {"message": "Dashboard data refreshed successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error refreshing dashboard: {str(e)}")

if __name__ == "__main__":
    print("Starting Invoice Processing API with MongoDB...")
    print("API will be available at: http://localhost:8000")
    print("API Documentation: http://localhost:8000/docs")
    print("Dashboard will be available at: http://localhost:8000/dash_app/")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)