"""Une tentative de creation d'instance Oracle A1. Pour GitHub Actions.

Sortie 0 = rien obtenu (normal, job vert).
Sortie 1 = INSTANCE OBTENUE -> GitHub envoie un mail d'echec de workflow,
           c'est volontaire, c'est la notification.
"""
import os, sys, oci

CONFIG = {
    "user":        os.environ["OCI_USER"],
    "tenancy":     os.environ["OCI_TENANCY"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region":      os.environ["OCI_REGION"],
    "key_file":    "oci_key.pem",
}
AD     = "Itte:EU-MARSEILLE-1-AD-1"
SUBNET = os.environ["OCI_SUBNET"]
IMAGE  = os.environ["OCI_IMAGE"]
SSHKEY = os.environ["OCI_SSH_KEY"]
TEN    = CONFIG["tenancy"]

cc = oci.core.ComputeClient(CONFIG)
vn = oci.core.VirtualNetworkClient(CONFIG)

# Garde-fou : ne jamais creer une deuxieme machine.
existing = [i for i in cc.list_instances(compartment_id=TEN).data
            if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
if existing:
    print(f"Instance deja presente : {existing[0].display_name} - rien a faire.")
    sys.exit(0)

details = oci.core.models.LaunchInstanceDetails(
    availability_domain=AD, compartment_id=TEN, display_name="SERV PERSO EVEN",
    shape="VM.Standard.A1.Flex",
    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12),
    source_details=oci.core.models.InstanceSourceViaImageDetails(
        image_id=IMAGE, boot_volume_size_in_gbs=150, boot_volume_vpus_per_gb=10),
    create_vnic_details=oci.core.models.CreateVnicDetails(
        subnet_id=SUBNET, assign_public_ip=True),
    metadata={"ssh_authorized_keys": SSHKEY},
)

try:
    inst = cc.launch_instance(details).data
except oci.exceptions.ServiceError as e:
    msg = (e.message or "").lower()
    if e.status == 429:
        print("Throttle Oracle (429)."); sys.exit(0)
    if e.status == 500 and "capacity" in msg:
        print("Pas de capacite disponible."); sys.exit(0)
    if e.status in (401, 500, 502, 503, 504):
        print(f"Erreur transitoire {e.status}."); sys.exit(0)
    print(f"ERREUR NON RECUPERABLE {e.status} {e.code} : {e.message}")
    sys.exit(1)

print(f"CAPACITE OBTENUE : {inst.id}")
inst = oci.wait_until(cc, cc.get_instance(inst.id), "lifecycle_state",
                      "RUNNING", max_wait_seconds=1200).data
ip = None
for va in cc.list_vnic_attachments(compartment_id=TEN, instance_id=inst.id).data:
    v = vn.get_vnic(va.vnic_id).data
    if v.public_ip:
        ip = v.public_ip

print("=" * 60)
print(f"  SERVEUR ORACLE OBTENU - IP PUBLIQUE : {ip}")
print(f"  ssh -i ~/.ssh/oracle_mc ubuntu@{ip}")
print("=" * 60)
sys.exit(1)   # volontaire : declenche le mail de notification GitHub
