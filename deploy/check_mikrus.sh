#!/usr/bin/env bash
# Szybka diagnostyka maszyny przed wdrożeniem OpenSearcha.
set -u

echo "=== RAM ==="
free -h 2>/dev/null || cat /proc/meminfo | head -3

echo "=== Wirtualizacja (KVM = możesz ustawić sysctl; LXC/OpenVZ = nie) ==="
if command -v systemd-detect-virt >/dev/null 2>&1; then
    systemd-detect-virt
else
    echo "systemd-detect-virt niedostępny; sprawdź /proc:"
    grep -i hypervisor /proc/cpuinfo >/dev/null 2>&1 && echo "wykryto hypervisor (prawdopodobnie KVM)" || echo "brak flagi hypervisor (prawdopodobnie kontener)"
fi

echo "=== vm.max_map_count (OpenSearch chce >= 262144) ==="
cat /proc/sys/vm/max_map_count 2>/dev/null || echo "nie odczytano"

echo "=== Dysk ==="
df -h / 2>/dev/null

echo "=== Docker ==="
docker --version 2>/dev/null || echo "Docker nie zainstalowany"

echo
echo "Wniosek:"
echo " - allow_mmap=false w compose działa BEZ zmiany max_map_count (bezpieczne na kontenerze)."
echo " - Jeśli to KVM i max_map_count < 262144, możesz: sysctl -w vm.max_map_count=262144,"
echo "   a w compose ustawić node.store.allow_mmap=true (lepsza wydajność)."
