<%
    cfg = dispatcher.call_sync('service.glusterd.get_config')
%>\
volume management
    type mgmt/glusterd
    option working-directory /var/db/glusterd
    option transport-type socket,rdma
    option transport.socket.keepalive-time 10
    option transport.socket.keepalive-interval 2
    option transport.socket.read-fail-log off
    option ping-timeout 30
#   option base-port 49152
end-volume
