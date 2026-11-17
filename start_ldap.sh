export NETLAPSE_LDAP_BIND_PASSWORD=svc-pw
python -m tools.ldapd --port 3389 -v
# restart the daemon in another shell
