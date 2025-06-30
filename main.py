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

# Import your existing classes
from invoice_extractor import InvoiceExtractor
from dashboard import app as dash_app

# Import the new GitHub storage class
from github_storage import GitHubCSVStorage, GitHubConfig

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
INVOICES_DIR = "invoices"
CSV_FILE = "invoice_data.csv"  # Local fallback file
PROCESSED_FILES_TRACKER = "processed_files.json"
extractor = InvoiceExtractor()

# GitHub storage instance (will be initialized on startup)
github_storage = None
use_github_storage = False

# Ensure directories exist
os.makedirs(INVOICES_DIR, exist_ok=True)

def initialize_github_storage():
    """Initialize GitHub storage if environment variables are available"""
    global github_storage, use_github_storage
    
    try:
        config = GitHubConfig()
        github_storage = config.get_storage_instance()
        use_github_storage = True
        print("✅ GitHub storage initialized successfully")
        return True
    except ValueError as e:
        print(f"⚠️  GitHub storage not configured: {e}")
        print("📁 Falling back to local CSV storage")
        use_github_storage = False
        return False
    except Exception as e:
        print(f"❌ Error initializing GitHub storage: {e}")
        print("📁 Falling back to local CSV storage")
        use_github_storage = False
        return False

def get_file_hash(file_path: str) -> str:
    """Generate a hash for a file to track if it's been processed"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def load_processed_files_tracker() -> dict:
    """Load the tracker of processed files"""
    if os.path.exists(PROCESSED_FILES_TRACKER):
        try:
            with open(PROCESSED_FILES_TRACKER, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading processed files tracker: {e}")
            return {}
    return {}

def save_processed_files_tracker(tracker: dict):
    """Save the tracker of processed files"""
    try:
        with open(PROCESSED_FILES_TRACKER, 'w') as f:
            json.dump(tracker, f, indent=2)
    except Exception as e:
        print(f"Error saving processed files tracker: {e}")

def is_file_processed(file_path: str) -> bool:
    """Check if a file has already been processed"""
    tracker = load_processed_files_tracker()
    filename = os.path.basename(file_path)
    
    if filename not in tracker:
        return False
    
    try:
        current_hash = get_file_hash(file_path)
        return tracker[filename] == current_hash
    except Exception as e:
        print(f"Error checking file hash for {filename}: {e}")
        return False

def mark_file_as_processed(file_path: str):
    """Mark a file as processed in the tracker"""
    tracker = load_processed_files_tracker()
    filename = os.path.basename(file_path)
    try:
        file_hash = get_file_hash(file_path)
        tracker[filename] = file_hash
        save_processed_files_tracker(tracker)
    except Exception as e:
        print(f"Error marking file as processed {filename}: {e}")

def remove_file_from_tracker(filename: str):
    """Remove a file from the processed files tracker"""
    tracker = load_processed_files_tracker()
    if filename in tracker:
        del tracker[filename]
        save_processed_files_tracker(tracker)
        print(f"Removed {filename} from processed files tracker")

def read_csv_data() -> pd.DataFrame:
    """Read CSV data from GitHub or local file"""
    global github_storage, use_github_storage
    
    if use_github_storage and github_storage:
        try:
            df = github_storage.read_csv_as_dataframe()
            if df is not None:
                print("📡 Successfully read CSV from GitHub")
                return df
            else:
                print("⚠️  No data found in GitHub, checking local file")
        except Exception as e:
            print(f"❌ Error reading from GitHub: {e}")
            print("📁 Falling back to local CSV")
    
    # Fallback to local file
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            print("📁 Successfully read local CSV file")
            return df
        except Exception as e:
            print(f"Error reading local CSV: {e}")
    
    # Return empty DataFrame with expected columns
    return pd.DataFrame(columns=extractor.csv_fields if hasattr(extractor, 'csv_fields') else [])

def append_to_csv(new_data: List[dict]):
    """Append new data to CSV (GitHub or local)"""
    global github_storage, use_github_storage
    
    if not new_data:
        return
    
    success = False
    
    # Try GitHub first if configured
    if use_github_storage and github_storage:
        try:
            success = github_storage.append_data_to_csv(new_data)
            if success:
                print(f"📡 Successfully added {len(new_data)} records to GitHub CSV")
            else:
                print("❌ Failed to update GitHub CSV, trying local fallback")
        except Exception as e:
            print(f"❌ Error updating GitHub CSV: {e}")
            print("📁 Falling back to local CSV")
    
    # Use local CSV if GitHub failed or not configured
    if not success:
        try:
            new_df = pd.DataFrame(new_data)
            
            if os.path.exists(CSV_FILE):
                # Append to existing CSV
                new_df.to_csv(CSV_FILE, mode='a', header=False, index=False)
            else:
                # Create new CSV with headers
                new_df.to_csv(CSV_FILE, index=False)
            
            print(f"📁 Added {len(new_data)} records to local CSV")
        except Exception as e:
            print(f"Error appending to local CSV: {e}")
            raise

def get_invoice_records_by_ids(invoice_ids: List[str]) -> tuple:
    """
    Get invoice records from CSV by invoice IDs
    Returns: (found_records, not_found_ids)
    """
    try:
        df = read_csv_data()
        
        if df.empty:
            return [], invoice_ids
        
        # Check if the CSV has an invoice_id column
        if 'invoice_id' not in df.columns:
            print("Warning: 'invoice_id' column not found in CSV. Using 'filename' instead.")
            id_column = 'filename' if 'filename' in df.columns else df.columns[0]
        else:
            id_column = 'invoice_id'
        
        found_records = []
        not_found_ids = []
        
        for invoice_id in invoice_ids:
            matching_records = df[df[id_column].astype(str) == str(invoice_id)]
            if not matching_records.empty:
                found_records.extend(matching_records.to_dict('records'))
            else:
                not_found_ids.append(invoice_id)
        
        return found_records, not_found_ids
        
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return [], invoice_ids

def delete_records_from_csv(invoice_ids: List[str]) -> int:
    """
    Delete records from CSV by invoice IDs
    Returns: number of deleted records
    """
    global github_storage, use_github_storage
    
    try:
        df = read_csv_data()
        
        if df.empty:
            return 0
        
        original_count = len(df)
        
        # Determine the ID column to use
        if 'invoice_id' not in df.columns:
            id_column = 'filename' if 'filename' in df.columns else df.columns[0]
        else:
            id_column = 'invoice_id'
        
        # Filter out records with matching IDs
        df_filtered = df[~df[id_column].astype(str).isin([str(id) for id in invoice_ids])]
        deleted_count = original_count - len(df_filtered)
        
        if deleted_count == 0:
            return 0
        
        # Update storage
        success = False
        
        # Try GitHub first if configured
        if use_github_storage and github_storage:
            try:
                success = github_storage.update_entire_csv(
                    df_filtered, 
                    f"Delete {deleted_count} invoice records"
                )
                if success:
                    print(f"📡 Successfully deleted {deleted_count} records from GitHub CSV")
            except Exception as e:
                print(f"❌ Error deleting from GitHub CSV: {e}")
                print("📁 Falling back to local CSV")
        
        # Use local CSV if GitHub failed or not configured
        if not success:
            df_filtered.to_csv(CSV_FILE, index=False)
            print(f"📁 Deleted {deleted_count} records from local CSV")
        
        return deleted_count
        
    except Exception as e:
        print(f"Error deleting records from CSV: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating CSV: {str(e)}")

def delete_pdf_files(filenames: List[str]) -> List[str]:
    """
    Delete PDF files from the invoices directory
    Returns: list of successfully deleted filenames
    """
    deleted_files = []
    
    for filename in filenames:
        file_path = os.path.join(INVOICES_DIR, filename)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted_files.append(filename)
                print(f"Deleted file: {filename}")
                
                # Remove from processed files tracker
                remove_file_from_tracker(filename)
            else:
                print(f"File not found: {filename}")
        except Exception as e:
            print(f"Error deleting file {filename}: {e}")
    
    return deleted_files

def initialize_csv_if_needed():
    """Initialize CSV file with headers if it doesn't exist"""
    global github_storage, use_github_storage
    
    # Check if we have data in GitHub
    if use_github_storage and github_storage:
        try:
            df = github_storage.read_csv_as_dataframe()
            if df is not None and not df.empty:
                print("✅ CSV data exists in GitHub repository")
                return
        except Exception as e:
            print(f"Error checking GitHub CSV: {e}")
    
    # Check local file
    if not os.path.exists(CSV_FILE):
        try:
            # Create empty CSV with headers
            df = pd.DataFrame(columns=extractor.csv_fields if hasattr(extractor, 'csv_fields') else [])
            df.to_csv(CSV_FILE, index=False)
            print("📁 Initialized local CSV file with headers")
            
            # Also upload to GitHub if configured
            if use_github_storage and github_storage:
                try:
                    github_storage.update_entire_csv(df, "Initialize CSV with headers")
                    print("📡 Initialized GitHub CSV file with headers")
                except Exception as e:
                    print(f"Warning: Could not initialize GitHub CSV: {e}")
                    
        except Exception as e:
            print(f"Error initializing CSV: {e}")

@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup"""
    print("🚀 Starting Invoice Processing API...")
    
    # Initialize GitHub storage
    initialize_github_storage()
    
    # Initialize CSV
    initialize_csv_if_needed()
    
    if use_github_storage:
        print("🌟 Running with GitHub CSV storage")
        if github_storage:
            raw_url = github_storage.get_raw_csv_url()
            print(f"📊 CSV URL: {raw_url}")
    else:
        print("📁 Running with local CSV storage")

@app.get("/")
async def root():
    """Root endpoint with API information"""
    storage_info = {
        "type": "GitHub" if use_github_storage else "Local",
        "csv_url": github_storage.get_raw_csv_url() if use_github_storage and github_storage else "local file"
    }
    
    return {
        "message": "Invoice Processing API",
        "storage": storage_info,
        "endpoints": {
            "upload_invoices": "/upload-invoices/",
            "process_directory": "/process-directory/",
            "delete_invoices": "/delete-invoices/",
            "get_data": "/data/",
            "dashboard": "/dash_app/",
            "health": "/health/",
            "csv_url": "/csv-url/"
        }
    }

@app.get("/csv-url/")
async def get_csv_url():
    """Get the URL where CSV data can be accessed"""
    if use_github_storage and github_storage:
        return {
            "csv_url": github_storage.get_raw_csv_url(),
            "storage_type": "GitHub",
            "note": "This URL provides direct access to the CSV data"
        }
    else:
        return {
            "csv_url": "Not available (using local storage)",
            "storage_type": "Local",
            "note": "CSV is stored locally and not accessible via URL"
        }

@app.get("/dashboard/")
async def get_dashboard():
    """Redirect directly to the dashboard"""
    return RedirectResponse(url="/dash_app/")

@app.delete("/delete-invoices/")
async def delete_invoices(request: DeleteInvoiceRequest):
    """
    Delete one or multiple invoices by their IDs
    This will remove both the PDF files and their records from the CSV
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
        
        # Delete records from CSV
        try:
            deleted_count = delete_records_from_csv(request.invoice_ids)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error deleting from CSV: {str(e)}")
        
        # Extract filenames from the found records and delete PDF files
        filenames_to_delete = []
        for record in found_records:
            filename = record.get('filename') or record.get('file_name') or record.get('source_file')
            if filename:
                if not filename.endswith('.pdf'):
                    filename += '.pdf'
                filenames_to_delete.append(filename)
        
        # Delete PDF files
        deleted_files = delete_pdf_files(filenames_to_delete)
        
        # Prepare response data
        for record in found_records:
            invoice_id = str(record.get('invoice_id', record.get('filename', 'unknown')))
            deleted_invoices.append({
                "invoice_id": invoice_id,
                "filename": record.get('filename', 'unknown'),
                "deleted_from_csv": True,
                "deleted_pdf_file": record.get('filename') in deleted_files if record.get('filename') else False
            })
        
        # Get remaining record count
        remaining_records = 0
        try:
            df = read_csv_data()
            remaining_records = len(df)
        except Exception as e:
            print(f"Error counting remaining records: {e}")
        
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
        df = read_csv_data()
        
        if df.empty:
            return {
                "message": "No invoice data found",
                "invoices": [],
                "total_count": 0
            }
        
        # Determine the ID column
        if 'invoice_id' not in df.columns:
            id_column = 'filename' if 'filename' in df.columns else df.columns[0]
        else:
            id_column = 'invoice_id'
        
        # Create a list of invoices with their basic info
        invoices = []
        for _, row in df.iterrows():
            invoice_info = {
                "id": str(row[id_column]),
                "filename": row.get('filename', 'unknown'),
            }
            # Add other relevant fields if they exist
            for field in ['invoice_number', 'date', 'total_amount', 'vendor']:
                if field in row and pd.notna(row[field]):
                    invoice_info[field] = row[field]
            
            invoices.append(invoice_info)
        
        return {
            "message": f"Found {len(invoices)} invoice(s)",
            "invoices": invoices,
            "total_count": len(invoices),
            "storage_type": "GitHub" if use_github_storage else "Local"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing invoices: {str(e)}")

@app.post("/upload-invoices/")
async def upload_invoices(files: Union[List[UploadFile], UploadFile] = File(...)):
    """
    Upload and process invoice PDFs (supports both single and multiple files)
    This endpoint handles both single file upload and multiple file upload
    """
    # Convert single file to list for uniform processing
    if isinstance(files, UploadFile):
        files = [files]
    
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    
    processed_files = []
    total_new_records = 0
    errors = []
    all_new_data = []
    skipped_files = []
    
    for file in files:
        if not file.filename.endswith('.pdf'):
            errors.append(f"{file.filename}: Only PDF files are allowed")
            continue
        
        file_path = os.path.join(INVOICES_DIR, file.filename)
        
        try:
            # Save the uploaded file
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            # Check if this file has already been processed
            if is_file_processed(file_path):
                skipped_files.append({
                    "filename": file.filename,
                    "reason": "Already processed (no changes detected)"
                })
                continue
            
            # Process the new/changed file
            print(f"Processing new/changed file: {file.filename}")
            invoice_data = extractor.process_invoice(file_path)
            
            if invoice_data:
                all_new_data.extend(invoice_data)
                mark_file_as_processed(file_path)
                
                processed_files.append({
                    "filename": file.filename,
                    "records_added": len(invoice_data)
                })
                total_new_records += len(invoice_data)
                print(f"Successfully processed {file.filename}: {len(invoice_data)} records")
            else:
                errors.append(f"{file.filename}: No data extracted")
                
        except Exception as e:
            error_msg = f"{file.filename}: {str(e)}"
            errors.append(error_msg)
            print(f"Error processing {file.filename}: {e}")
            
            # Clean up file on error
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
    
    # Append all new data to CSV in one operation
    if all_new_data:
        try:
            append_to_csv(all_new_data)
            print(f"Added {len(all_new_data)} total records to CSV")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error saving data to CSV: {str(e)}")
    
    # Get current total records
    total_records = 0
    try:
        df = read_csv_data()
        total_records = len(df)
    except Exception as e:
        print(f"Error reading CSV for count: {e}")
    
    return {
        "message": f"Upload completed. Processed {len(processed_files)} new/changed files",
        "processed_files": processed_files,
        "skipped_files": skipped_files,
        "total_new_records": total_new_records,
        "total_records": total_records,
        "storage_type": "GitHub" if use_github_storage else "Local",
        "errors": errors if errors else None
    }

@app.get("/health/")
async def health_check():
    """Health check endpoint"""
    try:
        # Get processing status
        pdf_count = 0
        processed_count = 0
        unprocessed_count = 0
        
        try:
            if os.path.exists(INVOICES_DIR):
                pdf_files = [f for f in os.listdir(INVOICES_DIR) if f.endswith('.pdf')]
                pdf_count = len(pdf_files)
                
                for filename in pdf_files:
                    file_path = os.path.join(INVOICES_DIR, filename)
                    if is_file_processed(file_path):
                        processed_count += 1
                    else:
                        unprocessed_count += 1
                        
        except Exception as e:
            print(f"Warning: Could not get processing status: {e}")
        
        # Get CSV record count
        csv_records = 0
        csv_accessible = False
        csv_location = "none"
        
        try:
            df = read_csv_data()
            csv_records = len(df)
            csv_accessible = True
            if use_github_storage:
                csv_location = "GitHub"
            else:
                csv_location = "Local"
        except Exception as e:
            print(f"Warning: Could not count CSV records: {e}")
        
        # GitHub storage status
        github_status = {
            "configured": use_github_storage,
            "accessible": False,
            "csv_url": None
        }
        
        if use_github_storage and github_storage:
            try:
                # Test GitHub connectivity
                content, sha = github_storage.get_file_content()
                github_status["accessible"] = True
                github_status["csv_url"] = github_storage.get_raw_csv_url()
            except Exception as e:
                print(f"GitHub storage not accessible: {e}")
        
        return {
            "status": "healthy",
            "message": "Invoice Processing API is running",
            "dashboard_status": "mounted at /dash_app/",
            "storage": {
                "type": csv_location,
                "csv_records": csv_records,
                "csv_accessible": csv_accessible,
                "github": github_status
            },
            "files": {
                "invoices_directory_exists": os.path.exists(INVOICES_DIR),
                "total_pdf_files": pdf_count,
                "processed_files": processed_count,
                "unprocessed_files": unprocessed_count
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Health check failed: {str(e)}"
        }

@app.get("/data/")
async def get_data():
    """Get CSV data for the dashboard or external use"""
    try:
        df = read_csv_data()
        
        if df.empty:
            return {
                "message": "No data available",
                "data": [],
                "total_records": 0,
                "storage_type": "GitHub" if use_github_storage else "Local"
            }
        
        # Convert DataFrame to list of dictionaries
        data = df.to_dict('records')
        
        return {
            "message": f"Successfully retrieved {len(data)} records",
            "data": data,
            "total_records": len(data),
            "storage_type": "GitHub" if use_github_storage else "Local",
            "csv_url": github_storage.get_raw_csv_url() if use_github_storage and github_storage else None
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving data: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Invoice Processing API...")
    print("API will be available at: http://localhost:8000")
    print("API Documentation: http://localhost:8000/docs")
    print("Dashboard will be available at: http://localhost:8000/dash_app/")
    
    # Check GitHub configuration
    try:
        config = GitHubConfig()
        print(f"✅ GitHub configured: {config.repo_owner}/{config.repo_name}")
    except ValueError as e:
        print(f"⚠️  GitHub not configured: {e}")
        print("📁 Will use local storage")
    
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
