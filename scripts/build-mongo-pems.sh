#!/usr/bin/env bash

set -euo pipefail

LE_LIVE_DIR=${LE_LIVE_DIR:-/etc/letsencrypt/live}
MONGO_OUTPUT_DIR=${MONGO_OUTPUT_DIR:-/etc/letsencrypt/service/mongo}
MONGO_ROOT_CA_SOURCE=${MONGO_ROOT_CA_SOURCE:-/etc/ssl/certs/ca-certificates.crt}
MONGO_UID=${MONGO_UID:-999}
MONGO_GID=${MONGO_GID:-999}

if [[ ! -d "$LE_LIVE_DIR" ]]; then
    echo "Certbot live directory not found: $LE_LIVE_DIR" >&2
    exit 1
fi
if [[ ! -r "$MONGO_ROOT_CA_SOURCE" ]]; then
    echo "CA bundle is missing or unreadable: $MONGO_ROOT_CA_SOURCE" >&2
    exit 1
fi

mkdir -p "$MONGO_OUTPUT_DIR"
shopt -s nullglob
cert_files=("$LE_LIVE_DIR"/*db*/fullchain.pem)
if (( ${#cert_files[@]} == 0 )); then
    echo "No Certbot fullchain.pem files found under $LE_LIVE_DIR" >&2
    exit 1
fi

declare -A best_cert=()
declare -A best_key=()
declare -A best_expiry=()

for cert_file in "${cert_files[@]}"; do
    key_file="${cert_file%/fullchain.pem}/privkey.pem"
    [[ -r "$key_file" ]] || continue
    openssl x509 -in "$cert_file" -checkend 0 -noout >/dev/null 2>&1 || continue

    cert_public_key=$(openssl x509 -in "$cert_file" -pubkey -noout 2>/dev/null | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
    key_public_key=$(openssl pkey -in "$key_file" -pubout -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
    [[ -n "$cert_public_key" && "$cert_public_key" == "$key_public_key" ]] || continue

    expiry=$(date -d "$(openssl x509 -in "$cert_file" -enddate -noout | cut -d= -f2-)" +%s)
    while IFS= read -r dns_name; do
        [[ "$dns_name" == \*.db.* ]] || continue
        suffix=${dns_name#*.db.}
        [[ "$suffix" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?){2,}$ ]] || continue

        if [[ -z "${best_expiry[$suffix]:-}" || $expiry -gt ${best_expiry[$suffix]} ]]; then
            best_cert[$suffix]=$cert_file
            best_key[$suffix]=$key_file
            best_expiry[$suffix]=$expiry
        fi
    done < <(openssl x509 -in "$cert_file" -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/.*DNS:\([^[:space:],]*\).*/\1/p')
done

if (( ${#best_cert[@]} == 0 )); then
    echo "No valid certificate with a SAN matching *.db.<environment>.<domain>.<tld> was found." >&2
    exit 1
fi

write_bundle() {
    local suffix=$1
    local cert_file=$2
    local key_file=$3
    local destination="$MONGO_OUTPUT_DIR/$suffix.pem"
    local temporary

    temporary=$(mktemp "$MONGO_OUTPUT_DIR/.${suffix}.pem.XXXXXX")
    cat "$cert_file" "$key_file" > "$temporary"
    chmod 0640 "$temporary"
    chown "$MONGO_UID:$MONGO_GID" "$temporary"

    if [[ -r "$destination" ]] && cmp -s "$temporary" "$destination"; then
        rm -f "$temporary"
        return
    fi

    mv -f "$temporary" "$destination"
    echo "Updated $destination from $cert_file"
}

write_ca_bundle() {
    local destination="$MONGO_OUTPUT_DIR/root.pem"
    local temporary

    temporary=$(mktemp "$MONGO_OUTPUT_DIR/.root.pem.XXXXXX")
    cp "$MONGO_ROOT_CA_SOURCE" "$temporary"
    chmod 0644 "$temporary"
    chown "$MONGO_UID:$MONGO_GID" "$temporary"

    if [[ -r "$destination" ]] && cmp -s "$temporary" "$destination"; then
        rm -f "$temporary"
        return
    fi

    mv -f "$temporary" "$destination"
    echo "Updated $destination from $MONGO_ROOT_CA_SOURCE"
}

for suffix in "${!best_cert[@]}"; do
    write_bundle "$suffix" "${best_cert[$suffix]}" "${best_key[$suffix]}"
done
write_ca_bundle
