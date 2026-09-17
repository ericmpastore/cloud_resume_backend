# Writing Numbers to Cloud SQL (MySQL) via a Cloud Run Function

**Architecture**

```
Browser (loads static site from GCS bucket behind HTTPS Load Balancer)
   │  client-side script.js
   ▼
POST https://REGION-PROJECT_ID.cloudfunctions.net/write-number
   │  Cloud Run function (Python, functions-framework, 2nd gen)
   ▼
Cloud SQL for MySQL  (existing instance, via Unix socket connection)
```

The browser loads `index.html`/`script.js` from a Cloud Storage bucket that sits behind an HTTPS Application Load Balancer. That script calls your Cloud Run function's HTTPS endpoint directly (a separate origin), which is why the function needs CORS headers. The function writes each number into a `numbers` table in your existing Cloud SQL MySQL instance.

Replace every `PROJECT_ID`, `REGION`, `INSTANCE_ID`, `yourdomain.com`, etc. below with your real values.

---

## 0. Prerequisites

- An existing Cloud SQL for MySQL instance (you confirmed you have one).
- Owner/Editor access on the GCP project, or equivalently: Cloud Functions Admin, Cloud SQL Admin, Storage Admin, Load Balancer Admin, IAM Admin.
- APIs enabled: **Cloud Run**, **Cloud Build**, **Artifact Registry**, **Cloud SQL Admin API**, **Compute Engine API** (for the load balancer). Enable any of these from **APIs & Services → Library** in the console if they're not already on.

### Terraform equivalent: project setup

Every section below also gets a **Terraform equivalent** subsection, showing how to build the same resource as code instead of clicking through the console. Use one approach or the other per resource — don't manage the same thing both ways, or `terraform apply` and your console clicks will fight each other.

Extra prerequisites for the Terraform path:
- [Terraform](https://developer.hashicorp.com/terraform/install) 1.5+ (or run it from Cloud Shell, which has it preinstalled).
- `gcloud auth application-default login` run once, so the Google provider can authenticate.
- A GCS bucket to hold Terraform's state file, created before `terraform init`:
  ```bash
  gcloud storage buckets create gs://PROJECT_ID-tfstate --location=REGION --uniform-bucket-level-access
  ```

Create a `terraform/` folder with a `providers.tf`:
```hcl
terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  backend "gcs" {
    bucket = "PROJECT_ID-tfstate"
    prefix = "cloud-resume"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "current" {
  project_id = var.project_id
}
```

and a `variables.tf` you'll keep adding to as later sections introduce new inputs:
```hcl
variable "project_id" {
  description = "Your GCP project ID"
  type        = string
}

variable "region" {
  description = "Region for all resources (match your Cloud SQL instance's region)"
  type        = string
  default     = "us-central1"
}
```

Run `terraform init` once in that folder. Every "Terraform equivalent" subsection below adds resource blocks to this same project — the file names given are just a suggestion; Terraform reads every `.tf` file in the directory regardless of name.

---

## 1. Prepare the database (existing Cloud SQL instance)

1. Console → **SQL** → click your instance → note the **Connection name** shown on the Overview page. It looks like `PROJECT_ID:REGION:INSTANCE_ID` — you'll need this exact string later.
2. Create a database (skip if you already have one):
   - Left menu → **Databases** → **Create database** → name it e.g. `numbers_db` → **Create**.
3. Create an application user (skip if reusing an existing one):
   - Left menu → **Users** → **Add user account** → Built-in authentication → username `numbers_app`, set a strong password → **Add**.
4. Create the table. Easiest path is **Cloud SQL Studio** (built into the console):
   - Left menu → **Cloud SQL Studio** → connect using the user/database from steps 2–3 → run:
   ```sql
   CREATE TABLE numbers (
     id INT AUTO_INCREMENT PRIMARY KEY,
     value DOUBLE NOT NULL,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   );
   ```

(Optional but recommended) Store the DB password in **Secret Manager** instead of a plain environment variable:
- Console → **Security → Secret Manager** → **Create secret** → name `db-password`, secret value = the password from step 3 → **Create secret**.

### Terraform equivalent: database, user, and secret

Reference your existing instance with a data source rather than letting Terraform try to create (and potentially destroy) it:

`sql.tf`
```hcl
variable "sql_instance_name" {
  description = "Name of your existing Cloud SQL instance"
  type        = string
}

data "google_sql_database_instance" "existing" {
  name = var.sql_instance_name
}

resource "google_sql_database" "numbers_db" {
  name     = "numbers_db"
  instance = data.google_sql_database_instance.existing.name
}

resource "google_sql_user" "numbers_app" {
  name     = "numbers_app"
  instance = data.google_sql_database_instance.existing.name
  password = var.db_password
}
```

`variables.tf` (add):
```hcl
variable "db_password" {
  description = "Password for the numbers_app MySQL user"
  type        = string
  sensitive   = true
}
```

Pass it at apply time instead of hardcoding it — e.g. `terraform apply -var="db_password=$(gcloud secrets versions access latest --secret=db-password)"`, or a `terraform.tfvars` file kept out of git.

Store the same password in Secret Manager, matching the optional step above, so the function can reference it as a secret rather than a plain environment variable:

`secrets.tf`
```hcl
resource "google_secret_manager_secret" "db_password" {
  secret_id = "db-password"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = var.db_password
}
```

**On the table itself:** the Google Terraform provider has no resource for MySQL tables/DDL — `google_sql_database` only creates the schema, not the tables inside it. Keep creating the `numbers` table via Cloud SQL Studio or a migration script as in step 4 above; shoehorning `CREATE TABLE` into Terraform (e.g. a `null_resource` running the Cloud SQL Auth Proxy + `mysql` client via a `local-exec` provisioner) works but is fragile — treat schema changes as a deliberate step outside `terraform apply`, not something it manages for you.

---

## 2. The Cloud Run function code

Create a local folder, e.g. `write-number-function/`, with two files. Cloud Run functions for Python require the source file to be named exactly `main.py` and dependencies listed in `requirements.txt`.

**`requirements.txt`**
```
functions-framework==3.*
PyMySQL==1.1.*
```

**`main.py`**
```python
import json
import os

import functions_framework
import pymysql

_connection = None


def get_connection():
    """Lazily open (and reuse, across warm invocations) a connection to Cloud SQL."""
    global _connection
    if _connection is None or not _connection.open:
        _connection = pymysql.connect(
            # Cloud Run mounts the Cloud SQL connection at this Unix socket path
            # once the instance is attached under the function's "Connections" tab.
            unix_socket=f"/cloudsql/{os.environ['INSTANCE_CONNECTION_NAME']}",
            user=os.environ['DB_USER'],
            password=os.environ['DB_PASS'],
            database=os.environ['DB_NAME'],
            autocommit=True,
        )
    return _connection


@functions_framework.http
def write_number(request):
    # CORS: allow the static site's origin (your load-balanced domain) to call this function.
    headers = {
        'Access-Control-Allow-Origin': os.environ.get('ALLOWED_ORIGIN', '*'),
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Content-Type': 'application/json',
    }

    if request.method == 'OPTIONS':
        # Preflight request
        return ('', 204, headers)

    if request.method != 'POST':
        return (json.dumps({'error': 'Only POST is supported.'}), 405, headers)

    body = request.get_json(silent=True) or {}
    try:
        value = float(body.get('value'))
    except (TypeError, ValueError):
        return (
            json.dumps({'error': 'Request body must include a numeric "value" field.'}),
            400,
            headers,
        )

    try:
        conn = get_connection()
        with conn.cursor() as cursor:
            cursor.execute('INSERT INTO numbers (value) VALUES (%s)', (value,))
            insert_id = cursor.lastrowid
        return (json.dumps({'success': True, 'insertId': insert_id, 'value': value}), 200, headers)
    except Exception as err:
        print(f'DB insert failed: {err}')
        return (json.dumps({'error': 'Failed to write to database.'}), 500, headers)
```

You can test this locally first:
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
functions-framework --target=write_number --debug
```
(It will fail to reach `/cloudsql/...` locally unless you also run the Cloud SQL Auth Proxy — that's expected; local testing is optional.)

### Terraform equivalent

No infrastructure to declare here — `main.py` and `requirements.txt` are application source, not something Terraform manages. The next section's Terraform equivalent picks these files up exactly as written, packaged into the same container image.

---

## 3. Deploy the function from the Cloud Console

1. Console → search **Cloud Run** → **Functions** tab (or go directly to *Cloud Run → Functions*) → **Write a function**.
2. Configuration:
   - **Environment**: Cloud Run function (2nd gen)
   - **Function name**: `write-number`
   - **Region**: the *same region* as your Cloud SQL instance
   - **Runtime**: Python 3.12
   - **Authentication**: "Allow unauthenticated invocations" (required so the browser can call it directly). See the security note in section 6 about the implications.
3. Click **Next**. In the inline source editor, replace the generated `main.py` and `requirements.txt` with the files from section 2. Set **Entry point** to `write_number`.
4. Before deploying, expand **Runtime, build, connections and security settings**:
   - **Variables & Secrets** tab → add runtime environment variables:
     | Name | Value |
     |---|---|
     | `DB_USER` | `numbers_app` |
     | `DB_NAME` | `numbers_db` |
     | `INSTANCE_CONNECTION_NAME` | `PROJECT_ID:REGION:INSTANCE_ID` |
     | `ALLOWED_ORIGIN` | `https://yourdomain.com` (set after section 5; use `*` temporarily for testing) |
     - For `DB_PASS`, prefer a **secret reference**: click **Reference a secret**, choose `db-password`, mount as environment variable `DB_PASS`. (Or add it as a plain variable if you skipped Secret Manager.)
   - **Connections** tab → **Cloud SQL connections** → **Add a connection** → select your existing instance.
5. Click **Deploy**. Once deployed, copy the **Trigger URL** shown at the top (e.g. `https://write-number-abcd1234-uc.a.run.app` or `https://REGION-PROJECT_ID.cloudfunctions.net/write-number`) — you'll use this in `script.js`.

### Terraform equivalent: deploying the function

Terraform's `google_cloudfunctions2_function` resource doesn't currently expose a field for attaching a Cloud SQL instance — the equivalent of the console's **Connections → Cloud SQL connections**, or `gcloud functions deploy --set-cloudsql-instances`. Since a 2nd-gen Cloud Run function *is* a Cloud Run service under the hood, the reliable way to get full Terraform coverage — Cloud SQL attachment included — is to manage it directly as a `google_cloud_run_v2_service` running the same container. It shows up in the same **Cloud Run** console page and behaves identically to what "Write a function" created.

That means you need a built container image either way. Reuse the `Dockerfile` from the Cloud Build troubleshooting section further down — you need it regardless of whether Cloud Build or Terraform ends up doing the deploying. Push it to **Artifact Registry** (Google shut down the older `gcr.io` Container Registry in March 2025, so new work should target `pkg.dev` repositories):
```bash
gcloud artifacts repositories create backend-images \
  --repository-format=docker --location=REGION
```

`function.tf`
```hcl
resource "google_service_account" "write_number" {
  account_id   = "write-number-sa"
  display_name = "Runtime service account for write-number"
}

resource "google_cloud_run_v2_service" "write_number" {
  name     = "write-number"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.write_number.email

    containers {
      image = "REGION-docker.pkg.dev/${var.project_id}/backend-images/write-number:latest"

      env {
        name  = "DB_USER"
        value = google_sql_user.numbers_app.name
      }
      env {
        name  = "DB_NAME"
        value = google_sql_database.numbers_db.name
      }
      env {
        name  = "INSTANCE_CONNECTION_NAME"
        value = data.google_sql_database_instance.existing.connection_name
      }
      env {
        name  = "ALLOWED_ORIGIN"
        value = "https://${var.domain}"
      }
      env {
        name = "DB_PASS"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_password.secret_id
            version = "latest"
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [data.google_sql_database_instance.existing.connection_name]
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# Equivalent of "Allow unauthenticated invocations"
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.write_number.location
  name     = google_cloud_run_v2_service.write_number.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
```

The `lifecycle { ignore_changes = [...] }` block matters once your Cloud Build trigger is also deploying new images on every push: without it, the next `terraform apply` would silently roll the service back to whatever image tag is hardcoded above, undoing your latest deploy. With it, Terraform manages everything about the service *except* which image is currently running — CI owns that.

`variables.tf` (add):
```hcl
variable "domain" {
  description = "Domain the frontend will be served from, for CORS"
  type        = string
}
```

---

## 4. Grant the function permission to reach Cloud SQL

1. Console → **IAM & Admin → IAM**.
2. Find the function's runtime service account — shown on the function's **Security** tab (by default `PROJECT_NUMBER-compute@developer.gserviceaccount.com`, unless you assigned a custom one).
3. Click the pencil/edit icon on that principal → **Add another role** → **Cloud SQL Client** (`roles/cloudsql.client`) → **Save**.

### Terraform equivalent

Already done in the previous block — but declared next to the service instead of clicked through separately, add these to `function.tf`:
```hcl
resource "google_project_iam_member" "write_number_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.write_number.email}"
}

resource "google_secret_manager_secret_iam_member" "write_number_secret_access" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.write_number.email}"
}
```
Unlike the console path, there's no separate manual IAM step to remember — it's declared right next to the resource that needs it, and `terraform apply` keeps it in sync going forward.

---

## 5. Client-side script + static site (hosted on GCS behind an HTTPS load balancer)

**`script.js`**
```js
const FUNCTION_URL = 'https://REGION-PROJECT_ID.cloudfunctions.net/write-number'; // trigger URL from section 3

async function sendNumber(value) {
  const res = await fetch(FUNCTION_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
  const data = await res.json();
  console.log('Response:', data);
  return data;
}

// Example: write a random number every 5 seconds.
// Replace with whatever logic actually produces your numbers.
setInterval(() => {
  const randomValue = Math.floor(Math.random() * 1000);
  sendNumber(randomValue).catch((err) => console.error('Failed to send number:', err));
}, 5000);
```

**`index.html`**
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Number Writer</title>
</head>
<body>
  <h1>Writing numbers to Cloud SQL…</h1>
  <p>Open the browser console to see responses.</p>
  <script src="script.js"></script>
</body>
</html>
```

### 5a. Create and populate the bucket

1. Console → **Cloud Storage → Buckets → Create**.
   - Name: a globally unique name, e.g. `numbers-writer-site-PROJECT_ID`.
   - Location: **Region**, matching your function's region (or a nearby multi-region).
   - Access control: **Uniform**.
   - Leave "Enforce public access prevention" **unchecked** (this bucket needs to be publicly readable to serve a website).
   - **Create**.
2. Open the bucket → **Upload files** → upload `index.html` and `script.js`.
3. Make the objects publicly readable: bucket → **Permissions** tab → **Grant access** → New principals: `allUsers` → Role: **Storage Object Viewer** → **Save** → confirm the public-access warning.
4. Set the default page for the bucket (console doesn't expose this setting — run it once from **Cloud Shell**, top-right `>_` icon in the console):
   ```bash
   gcloud storage buckets update gs://numbers-writer-site-PROJECT_ID --web-main-page-suffix=index.html
   ```

#### Terraform equivalent

```hcl
resource "google_storage_bucket" "site" {
  name                        = var.frontend_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true

  website {
    main_page_suffix = "index.html"
  }
}

resource "google_storage_bucket_iam_member" "public_read" {
  bucket = google_storage_bucket.site.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}
```
`variables.tf` (add):
```hcl
variable "frontend_bucket_name" {
  description = "Globally unique bucket name for the static site"
  type        = string
}
```
Skip uploading `index.html`/`script.js` through Terraform — leave that to the Cloud Build trigger in 5d's Terraform equivalent below, which is what actually keeps the bucket's contents current after every push. Having both Terraform and a CI sync fighting over the same objects just produces drift warnings on every `terraform plan`.

### 5b. Create the HTTPS Load Balancer

1. Console → **Network Services → Load Balancing → Create load balancer**.
2. Choose **Application Load Balancer (HTTP/HTTPS)** → **From internet to my backends** → **External Application Load Balancer** (Global, external, managed) → **Configure**.
3. **Backend configuration** → **Backend buckets** → **Create a backend bucket**:
   - Name: `numbers-site-backend`
   - Cloud Storage bucket: select the bucket from 5a
   - Enable Cloud CDN: optional
4. **Host and path rules**: leave the default rule pointing all traffic to `numbers-site-backend`.
5. **Frontend configuration**:
   - Protocol: **HTTPS**
   - IP address: create a new **static external IP**
   - Certificate: **Create a new certificate** → **Google-managed certificate** → enter your domain, e.g. `numbers.yourdomain.com`
6. **Review and finalize** → **Create**.
7. Once created, open the load balancer to find its **static IP address**. In your domain's DNS settings, create an **A record** for `numbers.yourdomain.com` pointing to that IP.
8. The Google-managed certificate provisions automatically once DNS resolves correctly — this can take anywhere from a few minutes to ~1 hour. The load balancer's detail page shows the certificate status.

#### Terraform equivalent

```hcl
resource "google_compute_backend_bucket" "site" {
  name        = "numbers-site-backend"
  bucket_name = google_storage_bucket.site.name
  enable_cdn  = false # set true to mirror the optional Cloud CDN step
}

resource "google_compute_global_address" "site" {
  name = "numbers-site-ip"
}

resource "google_compute_managed_ssl_certificate" "site" {
  name = "numbers-site-cert"
  managed {
    domains = [var.domain]
  }
}

resource "google_compute_url_map" "site" {
  name            = "numbers-site-url-map"
  default_service = google_compute_backend_bucket.site.id
}

resource "google_compute_target_https_proxy" "site" {
  name             = "numbers-site-https-proxy"
  url_map          = google_compute_url_map.site.id
  ssl_certificates = [google_compute_managed_ssl_certificate.site.id]
}

resource "google_compute_global_forwarding_rule" "site" {
  name                  = "numbers-site-forwarding-rule"
  target                = google_compute_target_https_proxy.site.id
  port_range            = "443"
  ip_address            = google_compute_global_address.site.id
  load_balancing_scheme = "EXTERNAL_MANAGED"
}

output "load_balancer_ip" {
  value = google_compute_global_address.site.address
}
```
After `terraform apply`, run `terraform output load_balancer_ip` to get the address for the DNS **A record** in step 7 above — Terraform provisions the load balancer and reserves the IP, but it can't create a record in a DNS zone it doesn't manage, so that step stays manual (or add a `google_dns_record_set` resource if your domain's zone is already in Cloud DNS). The managed certificate still takes the same up-to-~60-minute provisioning time once DNS resolves.

### 5c. Lock down CORS

Once your domain is live, go back to the function (**Cloud Run → Functions → write-number → Edit & deploy new revision**) and set `ALLOWED_ORIGIN` to your real domain, e.g. `https://numbers.yourdomain.com`, replacing any temporary `*` value. Deploy the new revision.

#### Terraform equivalent

`ALLOWED_ORIGIN` is already wired to `var.domain` in the Cloud Run service block from section 3's Terraform equivalent. Confirm that variable's value and run `terraform apply` again — Terraform deploys a new revision with the updated environment variable the same way "Edit & deploy new revision" does.

### 5d. Auto-deploy the frontend: sync the bucket on every push to the frontend repo

This mirrors what you already have for the backend, but instead of building a container, the trigger just copies the repo's files into the bucket with `gcloud storage rsync`.

1. **Connect the frontend repo to Cloud Build** (skip if it's already connected — e.g. if both repos are under the same GitHub account/org and you connected it once already): Console → **Cloud Build → Triggers → Connect repository** → choose **GitHub** → authorize/select your `cloud_resume_frontend`-style repo → **Connect**.
2. **Grant Cloud Build permission to write to the bucket.** Cloud Build runs as a service account (by default `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` — check the exact identity under **Cloud Build → Settings**). Grant it access scoped to just this bucket rather than the whole project:
   - Console → **Cloud Storage → Buckets** → open your site bucket → **Permissions** tab → **Grant access** → New principal: the Cloud Build service account email → Role: **Storage Object Admin** (`roles/storage.objectAdmin`) → **Save**.
3. **Add a `cloudbuild.yaml`** to the root of the frontend repo:
   ```yaml
   steps:
     - name: 'gcr.io/cloud-builders/gcloud'
       entrypoint: 'gcloud'
       args:
         - 'storage'
         - 'rsync'
         - '.'
         - 'gs://numbers-writer-site-PROJECT_ID'
         - '--recursive'
         - '--delete-unmatched-destination-objects'
         - '--exclude=^(\.git/.*|cloudbuild\.yaml)$'
   ```
   - `--recursive` copies subfolders too.
   - `--delete-unmatched-destination-objects` removes files from the bucket that no longer exist in the repo, so the bucket stays a true mirror.
   - `--exclude` keeps the `.git` folder and the `cloudbuild.yaml` file itself out of the public bucket.
4. **Create the trigger**: Console → **Cloud Build → Triggers → Create trigger**.
   - Source: the frontend repo you connected in step 1.
   - Event: **Push to a branch**, branch regex `^main$` (or whatever branch you deploy from).
   - Configuration: **Cloud Build configuration file (yaml or json)**, location `/cloudbuild.yaml`.
   - **Create**.
5. **Test it**: commit a small change to `index.html` or `script.js`, push to the branch, then watch **Cloud Build → History** for the new build. Once it succeeds, refresh `https://numbers.yourdomain.com` (hard-refresh / incognito, since browsers cache static assets) and confirm the change shows up.

**If you enabled Cloud CDN** on the backend bucket in section 5b, pushing new files won't immediately show up for visitors — Cloud CDN keeps serving cached copies until they expire or are invalidated. Add an extra step to the same `cloudbuild.yaml` to invalidate the cache on every deploy:
```yaml
   - name: 'gcr.io/cloud-builders/gcloud'
     entrypoint: 'gcloud'
     args:
       - 'compute'
       - 'url-maps'
       - 'invalidate-cdn-cache'
       - 'YOUR_URL_MAP_NAME'
       - '--path'
       - '/*'
       - '--global'
```
Find `YOUR_URL_MAP_NAME` under **Network Services → Load Balancing → your load balancer**. This step needs the Cloud Build service account to also have the **Compute Load Balancer Admin** role (`roles/compute.loadBalancerAdmin`), granted at the project level via **IAM & Admin → IAM**.

#### Terraform equivalent

A `google_cloudbuild_trigger` replaces the console's **Create trigger** step, pointing at the same `cloudbuild.yaml` you already wrote above:

```hcl
resource "google_cloudbuild_trigger" "frontend_deploy" {
  name     = "frontend-deploy"
  location = "global"
  filename = "cloudbuild.yaml"

  github {
    owner = var.github_owner
    name  = var.frontend_repo_name
    push {
      branch = "^main$"
    }
  }
}

resource "google_storage_bucket_iam_member" "cloudbuild_bucket_writer" {
  bucket = google_storage_bucket.site.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}
```
`variables.tf` (add):
```hcl
variable "github_owner" {
  description = "GitHub username or org that owns both repos"
  type        = string
}

variable "frontend_repo_name" {
  description = "Name of the frontend GitHub repository"
  type        = string
}
```

The same pattern covers the backend trigger from section 3, if you want Terraform to own that one too — this mirrors the manual-fallback IAM grants from the trigger-recreation troubleshooting section further down, just declared once instead of clicked through by hand:
```hcl
variable "backend_repo_name" {
  description = "Name of the backend GitHub repository"
  type        = string
}

resource "google_cloudbuild_trigger" "backend_deploy" {
  name     = "backend-deploy"
  location = "global"
  filename = "cloudbuild.yaml" # the docker build + gcloud run deploy steps from the troubleshooting section below

  github {
    owner = var.github_owner
    name  = var.backend_repo_name
    push {
      branch = "^main$"
    }
  }
}

resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_service_account_iam_member" "cloudbuild_act_as" {
  service_account_id = google_service_account.write_number.name
  role                = "roles/iam.serviceAccountUser"
  member              = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}
```

Both GitHub repos still need to be connected to Cloud Build (the console's one-time "Connect repository" OAuth step) before Terraform can create triggers against them — Terraform can declare the trigger, but that initial GitHub authorization stays a manual, one-time click.

---

## 6. Verify end to end

1. **Test the function directly** from Cloud Shell:
   ```bash
   curl -X POST https://REGION-PROJECT_ID.cloudfunctions.net/write-number \
     -H "Content-Type: application/json" \
     -d '{"value": 42}'
   ```
   Expect `{"success":true,"insertId":1,"value":42}`.
2. **Check the database** via Cloud SQL Studio or Cloud Shell:
   ```sql
   SELECT * FROM numbers ORDER BY id DESC LIMIT 10;
   ```
3. **Test the full path**: visit `https://numbers.yourdomain.com`, open the browser dev console, and confirm you see periodic `Response: {success: true, ...}` logs, and that new rows keep appearing in the `numbers` table.

### Terraform equivalent

Verification doesn't change — `curl`, the SQL query, and the browser test all work the same regardless of how the infrastructure was created. Two Terraform-specific checks worth adding:
- `terraform plan` should come back with **no changes** once everything is applied and CI has deployed at least once — if it wants to change `template[0].containers[0].image` on the Cloud Run service, the `ignore_changes` lifecycle block from section 3's Terraform equivalent is missing or misconfigured.
- `terraform state list` should show every resource from these sections (`google_cloud_run_v2_service.write_number`, `google_sql_database.numbers_db`, `google_storage_bucket.site`, the load balancer resources, both triggers) — anything missing means it's still only living in the console and Terraform doesn't know about it.

---

## Security notes

- **Unauthenticated invocation is required** here because a browser (not a trusted backend) calls the function directly — but it also means anyone who discovers the function URL can POST arbitrary values. Consider adding **Cloud Armor** (rate limiting / IP allowlisting) by putting the function behind the same load balancer as a serverless NEG backend, or adding a simple shared-secret header check in `index.js`, if this will run somewhere besides a quick test.
- Prefer the **Secret Manager** reference for `DB_PASS` over a plain environment variable (section 3).
- The `numbers_app` MySQL user only needs `INSERT` (and `SELECT`, for the verification query) privileges on the `numbers` table — avoid granting it broader access than that.
- A publicly readable Cloud Storage bucket is fine for static site assets (HTML/JS) but never put credentials or sensitive data in it.
- **Terraform-specific:** your state file (in the `PROJECT_ID-tfstate` bucket) contains `db_password` in plain text, because Terraform needs to track it. Lock that bucket down as tightly as the database itself — no `allUsers` access, IAM restricted to whoever/whatever runs `terraform apply` — and never commit a `terraform.tfvars` file containing real secrets to git.

---

## Troubleshooting: Cloud Build error — "unable to evaluate symlinks in Dockerfile path"

```
Step #0 - "Build": unable to prepare context: unable to evaluate symlinks in Dockerfile path: lstat /workspace/Dockerfile: no such file or directory
```

**What's happening:** this build is coming from a Cloud Build trigger connected to your GitHub repo (`cloud_resume_backend`), separate from the console's inline-editor deploy in section 3. That trigger's build step is `gcr.io/cloud-builders/docker build ...` — a *raw Docker build* — which requires a file literally named `Dockerfile` at the root of the repo. Your repo doesn't have one yet, so Cloud Build fetches the source fine but fails immediately when it tries to build the image.

**Fix: add a `Dockerfile` to the repo root**, alongside `main.py` and `requirements.txt` from section 2.

**`Dockerfile`**
```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects PORT at runtime; functions-framework listens on it.
ENV PORT=8080
# Must match the entry-point function name in main.py (section 2).
ENV FUNCTION_TARGET=write_number

CMD exec functions-framework --target=${FUNCTION_TARGET} --port=${PORT}
```

Steps:
1. In your local clone of `cloud_resume_backend`, add the `Dockerfile` above at the repo root (same directory as `main.py` and `requirements.txt`).
2. (Optional) Test it locally before pushing:
   ```bash
   docker build -t write-number-test .
   docker run -p 8080:8080 \
     -e DB_USER=numbers_app -e DB_PASS=yourpassword \
     -e DB_NAME=numbers_db -e INSTANCE_CONNECTION_NAME=PROJECT_ID:REGION:INSTANCE_ID \
     write-number-test
   ```
   Note: the `/cloudsql/...` socket won't exist locally, so DB calls will fail unless you also run the [Cloud SQL Auth Proxy](https://cloud.google.com/sql/docs/mysql/sql-proxy) — the container starting and responding to a request at all is enough to confirm the Dockerfile itself is correct.
3. Commit and push:
   ```bash
   git add Dockerfile
   git commit -m "Add Dockerfile for Cloud Build"
   git push origin main
   ```
4. Console → **Cloud Build → Triggers** → find the trigger for this repo → confirm **Dockerfile directory** is `/` and **Dockerfile name** is `Dockerfile` (the defaults). If a build didn't start automatically from the push, click **Run** on the trigger manually.
5. Console → **Cloud Build → History** → confirm the new build reaches the end. If there's a deploy step after the build (check the trigger's config or an accompanying `cloudbuild.yaml` in the repo), verify it also succeeds and that the new revision shows up on the function in **Cloud Run → Functions → write-number**.

If instead you'd rather not maintain a Dockerfile at all, the simpler long-term option is switching that trigger's **Build Configuration** from "Dockerfile" to "Buildpacks" (or dropping the GitHub trigger entirely and redeploying via the console's inline editor from section 3) — that path builds the Python source directly, the same way Cloud Run functions normally work, with no Dockerfile required.

### Terraform equivalent

This specific error — a missing `Dockerfile` — happens the same way regardless of who creates the trigger; it's a repo-content problem, not a Terraform problem, so the fix above (commit the `Dockerfile`) is unchanged. Terraform only enters the picture if your `cloudbuild.yaml`'s deploy step targets a Cloud Run service Terraform also manages: make sure that service already has the `lifecycle { ignore_changes = [...] }` block from section 3's Terraform equivalent, or a `terraform apply` run after this fix could revert the image CI just deployed.

---

## Troubleshooting: recreating a deleted Cloud Build trigger for an existing Cloud Run resource

If your backend's Cloud Build trigger gets deleted (accidentally or otherwise), builds stop firing on push, but the existing `write-number` resource itself is untouched — its current revision, environment variables/secrets, Cloud SQL connection, and IAM roles (e.g. `roles/cloudsql.client` on its runtime service account) all stay exactly as they were. You just need a new trigger wired back to the same resource.

**Recommended: reconnect from the resource's own page (handles IAM for you)**
1. Console → **Cloud Run** → click into your `write-number` resource.
2. On its details page, click **Connect to repo** (this may also appear as **Set up Continuous Deployment**, depending on console version).
3. Select the source: your `cloud_resume_backend` GitHub repo → branch (e.g. `^main$`) → **Build type: Dockerfile** (since the repo now has the `Dockerfile` from the fix above) → build context `/`.
4. **Save**. Google Cloud creates a brand-new Cloud Build trigger and — critically — automatically grants the Cloud Build service account the roles it needs to deploy into this *specific existing resource*: **Cloud Build Service Account**, **Cloud Run Admin**, and **Service Account User** (plus a couple of Developer Connect roles if your GitHub connection uses the newer GitHub App-based integration). You don't need to grant these by hand.
5. Push a small commit to confirm: watch **Cloud Build → History** for a new build, then confirm a new revision lands on `write-number`.

**Manual fallback** (if "Connect to repo" isn't available, or you want full control over the build/deploy steps):
1. Console → **Cloud Build → Triggers → Create trigger** → select the repo, branch, and configuration file location (`/cloudbuild.yaml` or `/Dockerfile` depending on how you want it built).
2. Make sure your `cloudbuild.yaml` (or the trigger's implicit build) ends with a step that deploys to the *existing* resource by name, e.g.:
   ```yaml
   - name: 'gcr.io/cloud-builders/gcloud'
     args: ['run', 'deploy', 'write-number', '--image', 'gcr.io/$PROJECT_ID/write-number:$COMMIT_SHA', '--region', 'REGION']
   ```
3. Grant the Cloud Build service account (`PROJECT_NUMBER@cloudbuild.gserviceaccount.com`) the **Service Account User** role (`roles/iam.serviceAccountUser`) on `write-number`'s runtime service account, and **Cloud Run Admin** (`roles/run.admin`) at the project level — otherwise the deploy step will fail with a permissions error even though the build itself succeeds.

Either way, you're reconnecting a *new* trigger to the *same* resource — nothing about `write-number` itself (its name, URL, env vars, Cloud SQL attachment) needs to be recreated.

### Terraform equivalent

If the deleted trigger was managed by Terraform, don't use the console's "Connect to repo" wizard to recreate it — that creates a second, console-managed trigger that Terraform doesn't know about, and the next `terraform apply` won't clean it up (or worse, will try to create a duplicate with the same name and fail). Instead:
1. Check whether it's still tracked in state: `terraform state list | grep cloudbuild_trigger`.
2. If it's gone from state too (someone ran `terraform state rm`, or deleted it outside Terraform entirely), just run `terraform apply` again — the trigger's resource block is still in your `.tf` files, so Terraform recreates it with the same configuration, IAM bindings included.
3. If it's still tracked in state but was deleted out-of-band in the console, `terraform plan` will show it as needing to be created again (Terraform detects the drift); `terraform apply` fixes it the same way.

This is the one scenario where Terraform is strictly less error-prone than the console: there's no "which button do I click" step to remember — just `terraform apply`.
