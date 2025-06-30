#!/usr/bin/env python3
"""
GitHub Token & Repository Debug Tool
Run this script to diagnose GitHub API token issues
"""

import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

def debug_github_access():
    """Comprehensive GitHub access debugging"""
    
    print("🔍 GitHub Access Debugging Tool")
    print("=" * 50)
    
    # Get environment variables
    repo_owner = os.getenv('GITHUB_REPO_OWNER')
    repo_name = os.getenv('GITHUB_REPO_NAME')
    token = os.getenv('GITHUB_TOKEN')
    
    print(f"📋 Configuration:")
    print(f"   GITHUB_REPO_OWNER: {repo_owner}")
    print(f"   GITHUB_REPO_NAME: {repo_name}")
    print(f"   GITHUB_TOKEN: {token[:10]}...{token[-5:] if token else 'None'}")
    print()
    
    if not all([repo_owner, repo_name, token]):
        print("❌ Missing required environment variables!")
        return
    
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'InvoiceProcessor/1.0'
    }
    
    # Test 1: Token validation
    print("🧪 Test 1: Token Validation")
    try:
        response = requests.get('https://api.github.com/user', headers=headers)
        if response.status_code == 200:
            user_data = response.json()
            print(f"   ✅ Token is valid")
            print(f"   👤 User: {user_data.get('login')}")
            print(f"   📧 Email: {user_data.get('email', 'Not public')}")
        else:
            print(f"   ❌ Token validation failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return
    except Exception as e:
        print(f"   ❌ Error validating token: {e}")
        return
    
    print()
    
    # Test 2: Repository access
    print("🧪 Test 2: Repository Access")
    repo_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}"
    try:
        response = requests.get(repo_url, headers=headers)
        if response.status_code == 200:
            repo_data = response.json()
            print(f"   ✅ Repository accessible")
            print(f"   📁 Name: {repo_data.get('full_name')}")
            print(f"   🔒 Private: {repo_data.get('private')}")
            print(f"   🌿 Default branch: {repo_data.get('default_branch')}")
            
            # Check permissions
            permissions = repo_data.get('permissions', {})
            print(f"   📝 Permissions:")
            print(f"      - Read: {permissions.get('pull', False)}")
            print(f"      - Write: {permissions.get('push', False)}")
            print(f"      - Admin: {permissions.get('admin', False)}")
            
        elif response.status_code == 404:
            print(f"   ❌ Repository not found or not accessible")
            print(f"   Check: https://github.com/{repo_owner}/{repo_name}")
        else:
            print(f"   ❌ Repository access failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return
    except Exception as e:
        print(f"   ❌ Error accessing repository: {e}")
        return
    
    print()
    
    # Test 3: Token scopes
    print("🧪 Test 3: Token Scopes")
    try:
        response = requests.get('https://api.github.com/user', headers=headers)
        scopes = response.headers.get('X-OAuth-Scopes', '')
        print(f"   📋 Token scopes: {scopes}")
        
        required_scopes = ['repo'] if repo_data.get('private') else ['public_repo']
        has_required = any(scope.strip() in scopes for scope in required_scopes)
        
        if has_required or 'repo' in scopes:
            print(f"   ✅ Token has required scopes")
        else:
            print(f"   ❌ Token missing required scopes")
            print(f"   🔧 Required: {required_scopes}")
            print(f"   💡 Recommendation: Create new token with 'repo' scope")
    except Exception as e:
        print(f"   ❌ Error checking scopes: {e}")
    
    print()
    
    # Test 4: Contents API access
    print("🧪 Test 4: Contents API Test")
    contents_url = f"{repo_url}/contents"
    try:
        response = requests.get(contents_url, headers=headers)
        if response.status_code == 200:
            print(f"   ✅ Contents API accessible")
            files = response.json()
            print(f"   📂 Repository has {len(files)} files in root")
        else:
            print(f"   ❌ Contents API failed: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"   ❌ Error accessing contents: {e}")
    
    print()
    
    # Test 5: File creation test
    print("🧪 Test 5: File Creation Test")
    test_file_url = f"{repo_url}/contents/test_access.txt"
    test_content = "VGVzdCBmaWxlIGZvciBhY2Nlc3MgdmVyaWZpY2F0aW9u"  # "Test file for access verification" in base64
    
    test_data = {
        'message': 'Test file creation for debugging',
        'content': test_content
    }
    
    try:
        # Try to create a test file
        response = requests.put(test_file_url, headers=headers, json=test_data)
        if response.status_code in [200, 201]:
            print(f"   ✅ File creation successful!")
            
            # Clean up - delete the test file
            file_data = response.json()
            delete_data = {
                'message': 'Clean up test file',
                'sha': file_data['content']['sha']
            }
            delete_response = requests.delete(test_file_url, headers=headers, json=delete_data)
            if delete_response.status_code == 200:
                print(f"   🧹 Test file cleaned up")
            
        else:
            print(f"   ❌ File creation failed: {response.status_code}")
            print(f"   Response: {response.text}")
            
            # Parse the error response
            try:
                error_data = response.json()
                error_msg = error_data.get('message', 'Unknown error')
                print(f"   📝 Error message: {error_msg}")
                
                if 'documentation_url' in error_data:
                    print(f"   📚 Documentation: {error_data['documentation_url']}")
                    
            except:
                pass
                
    except Exception as e:
        print(f"   ❌ Error in file creation test: {e}")
    
    print()
    
    # Test 6: Organization/SSO check
    print("🧪 Test 6: Organization & SSO Check")
    try:
        if '/' in repo_owner and repo_owner != user_data.get('login'):
            # This might be an organization repo
            org_url = f"https://api.github.com/orgs/{repo_owner.split('/')[0]}"
            response = requests.get(org_url, headers=headers)
            if response.status_code == 200:
                print(f"   🏢 Organization repository detected")
                print(f"   ⚠️  Check if SSO is required for organization access")
                print(f"   🔗 Go to: https://github.com/settings/tokens")
                print(f"   📋 Look for 'Configure SSO' next to your token")
    except Exception as e:
        print(f"   ⚠️  Could not check organization settings: {e}")
    
    print()
    print("🎯 Summary & Recommendations:")
    print("-" * 30)
    
    if response.status_code in [200, 201]:
        print("✅ Your token should work! The issue might be elsewhere.")
    else:
        print("❌ Token has issues. Try these solutions:")
        print("   1. Create a new Personal Access Token with 'repo' scope")
        print("   2. If using organization repo, configure SSO")
        print("   3. Make sure you're the owner or collaborator of the repository")
        print("   4. Try using Fine-grained Personal Access Token instead")
    
    print(f"\n🔗 Useful links:")
    print(f"   Token settings: https://github.com/settings/tokens")
    print(f"   Repository: https://github.com/{repo_owner}/{repo_name}")
    print(f"   Repository settings: https://github.com/{repo_owner}/{repo_name}/settings")

if __name__ == "__main__":
    debug_github_access()