% for ntp in dispatcher.call_sync('ntpservers.query'):
server ${ntp['address']}\
% if ntp.get('burst'):
 burst\
% endif
% if ntp.get('iburst'):
 iburst\
% endif
% if ntp.get('prefer'):
 prefer\
% endif
% if ntp.get('maxpoll') is not None:
 maxpoll ${ntp['maxpoll']}\
% endif
% if ntp.get('minpoll') is not None:
 minpoll ${ntp['minpoll']}\
% endif

% endfor
restrict -4 default nomodify nopeer noquery notrap
restrict -6 default nomodify nopeer noquery notrap
restrict 127.0.0.1
restrict -6 ::1
restrict 127.127.1.0
