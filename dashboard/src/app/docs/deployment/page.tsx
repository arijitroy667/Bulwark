import type { Metadata } from 'next';
import Link from 'next/link';
import { Code, DocHeader, Flow, Table } from '@/components/docs';

export const metadata: Metadata = { title: 'Deployment' };

export default function Deployment() {
  return (
    <>
      <DocHeader
        eyebrow="Reference"
        title="Deployment"
        lead="The production path runs on Google Cloud: a public webhook receiver, a Pub/Sub queue and an orchestrator worker that builds and replays each merge request in its own Cloud Run service."
      />

      <Flow
        items={[
          ['Webhook receiver', 'Cloud Run, public'],
          ['bulwark-mr-events', 'Pub/Sub topic'],
          ['Push subscription', 'OIDC-authenticated'],
          ['Orchestrator worker', 'Cloud Run'],
          ['bulwark-sim-<id>', 'per-MR Cloud Run'],
        ]}
      />

      <h2 id="prerequisites">Prerequisites</h2>
      <ul>
        <li>A Google Cloud project with billing, plus <code>gcloud</code>, Docker and Terraform</li>
        <li>A MongoDB deployment reachable from Cloud Run</li>
        <li>A GitLab access token, a Dynatrace OAuth client, an Arize API key and a Gemini key (see <Link href="/docs/integrations">Integrations</Link>)</li>
        <li>A service instrumented in Dynatrace with at least 50 request logs in the last 24 hours</li>
      </ul>

      <h2 id="terraform">What Terraform creates</h2>
      <Table
        head={['Resource', 'Purpose']}
        rows={[
          ['Project APIs', 'Cloud Run, Cloud Build, Pub/Sub, Artifact Registry, Secret Manager'],
          [<code key="1">bulwark-orchestrator</code>, 'Service account with run, pubsub, secretmanager, artifactregistry, cloudbuild and logging roles'],
          [<code key="2">bulwark-images</code>, 'Artifact Registry repository for Bulwark and simulation images'],
          [<code key="3">bulwark-mr-events</code>, 'Pub/Sub topic, with a push subscription to /pubsub/orchestrate and a bulwark-dead-letter topic'],
          [<code key="4">bulwark-webhook-receiver</code>, 'Cloud Run service, publicly invokable'],
          [<code key="5">bulwark-orchestrator-worker</code>, 'Cloud Run service invoked by Pub/Sub'],
          ['Secrets', 'GitLab, Dynatrace, Arize, MongoDB and Gemini credentials in Secret Manager'],
        ]}
      />

      <h2 id="steps">Steps</h2>
      <h3>1. Create the image repository, then build and push the images</h3>
      <p>Cloud Run needs the images to exist, so the registry comes first.</p>
      <Code>{`cd terraform
terraform init
terraform apply -target=google_artifact_registry_repository.bulwark_images
cd ..

REG=us-central1-docker.pkg.dev/<project>/bulwark-images
docker build -f webhook_receiver/Dockerfile -t $REG/webhook-receiver:latest .
docker build -f orchestrator/Dockerfile     -t $REG/orchestrator-worker:latest .
docker push $REG/webhook-receiver:latest
docker push $REG/orchestrator-worker:latest`}</Code>

      <h3>2. Create the build-source bucket</h3>
      <p>Branch archives are staged here for Cloud Build. Terraform does not create it.</p>
      <Code>{`gcloud storage buckets create gs://bulwark-build-sources --location=us-central1
gcloud storage buckets add-iam-policy-binding gs://bulwark-build-sources \\
  --member=serviceAccount:bulwark-orchestrator@<project>.iam.gserviceaccount.com \\
  --role=roles/storage.objectAdmin`}</Code>

      <h3>3. Apply the rest</h3>
      <p>
        Fill in <code>terraform/terraform.tfvars</code> — project, region and the credentials listed in{' '}
        <code>variables.tf</code> — then:
      </p>
      <Code>{`terraform apply`}</Code>

      <h3>4. Connect GitLab</h3>
      <p>
        Add a webhook on the project pointing at{' '}
        <code>https://&lt;bulwark-webhook-receiver&gt;/webhook/gitlab</code> with the same secret as{' '}
        <code>GITLAB_WEBHOOK_TOKEN</code> and <em>Merge request events</em> enabled. To make the gate mandatory, require
        the <code>Bulwark/production-ready</code> status before merging.
      </p>

      <h3>5. Verify</h3>
      <Code>{`curl https://<bulwark-webhook-receiver>/health
curl https://<bulwark-orchestrator-worker>/health`}</Code>
      <p>
        Open a merge request: a simulation appears in the dashboard history, and the verdict comment and commit status
        appear on the MR when it finishes.
      </p>

      <h2 id="endpoints">Endpoints</h2>
      <Table
        head={['Service', 'Endpoint', 'Purpose']}
        rows={[
          ['Webhook receiver', <code key="1">POST /webhook/gitlab</code>, 'GitLab merge request events'],
          ['Webhook receiver', <code key="2">GET /health</code>, 'Liveness'],
          ['Orchestrator', <code key="3">POST /pubsub/orchestrate</code>, 'Pub/Sub push; acknowledges at once, runs in the background'],
          ['Orchestrator', <code key="4">GET /api/simulations</code>, 'Recent simulations'],
          ['Orchestrator', <code key="5">GET /api/simulation/:id[/stream]</code>, 'One simulation; /stream is server-sent events'],
          ['Orchestrator', <code key="6">POST /api/setup</code>, 'Store a token and register the GitLab webhook'],
          ['Orchestrator', <code key="7">POST /api/demo/simulations</code>, 'Start a demo scenario (demo mode only)'],
        ]}
      />

      <h2 id="security">Security</h2>
      <ul>
        <li>Webhooks without the right <code>X-Gitlab-Token</code> are rejected.</li>
        <li>Simulations never receive production credentials, run as a single instance and are deleted afterwards — image and source archive included.</li>
        <li>Production requests are anonymised before replay, storage or transmission.</li>
        <li>Simulation records expire after 90 days.</li>
      </ul>
    </>
  );
}
