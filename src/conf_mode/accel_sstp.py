#!/usr/bin/env python3
#
# Copyright (C) 2018 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

import sys
import os
import re
import subprocess
import jinja2
import socket
import time
import syslog as sl

from vyos.config import Config
from vyos import ConfigError

pidfile = r'/var/run/accel_sstp.pid'
sstp_cnf_dir = r'/etc/accel-ppp/sstp'
chap_secrets = sstp_cnf_dir + '/chap-secrets'
sstp_conf = sstp_cnf_dir + '/sstp.config'
ssl_cert_dir = r'/config/user-data/sstp'

### config path creation
if not os.path.exists(sstp_cnf_dir):
  os.makedirs(sstp_cnf_dir)
  sl.syslog(sl.LOG_NOTICE, sstp_cnf_dir  + " created")

if not os.path.exists(ssl_cert_dir):
  os.makedirs(ssl_cert_dir)
  sl.syslog(sl.LOG_NOTICE, ssl_cert_dir  + " created")

sstp_config = '''
### generated by accel_sstp.py ### 
[modules]
log_syslog
sstp
ippool
shaper
{% if authentication['mode'] == 'local' %}
chap-secrets
{% endif -%}
{% for proto in authentication['auth_proto'] %}
{{proto}}
{% endfor %}
{% if authentication['mode'] == 'radius' %}
radius
{% endif %}

[core]
thread-count={{thread_cnt}}

[common]
single-session=replace

[log]
syslog=accel-sstp,daemon
copy=1
level=5

[client-ip-range]
disable

[sstp]
verbose=1
accept=ssl
{% if certs %}
ssl-ca-file=/config/user-data/sstp/{{certs['ca']}}
ssl-pemfile=/config/user-data/sstp/{{certs['server-cert']}}
ssl-keyfile=/config/user-data/sstp/{{certs['server-key']}}
{% endif %}

{%if ip_pool %}
[ip-pool]
gw-ip-address={{gw}}
{% for sn in ip_pool %}
{{sn}}
{% endfor %}
{% endif %}

{% if dnsv4 %}
[dns]
{% if dnsv4['primary'] %}
dns1={{dnsv4['primary']}}
{% endif -%}
{% if dnsv4['secondary'] %}
dns2={{dnsv4['secondary']}}
{% endif -%}
{% endif %}

{% if authentication['mode'] == 'local' %}
[chap-secrets]
chap-secrets=/etc/accel-ppp/sstp/chap-secrets
{% endif %}

{%- if authentication['mode'] == 'radius' %}
[radius]
verbose=1
{% for rsrv in authentication['radius-srv']: %}
server={{rsrv}},{{authentication['radius-srv'][rsrv]['secret']}},\
req-limit={{authentication['radius-srv'][rsrv]['req-limit']}},\
fail-time={{authentication['radius-srv'][rsrv]['fail-time']}}
{% endfor -%}
{% if authentication['radiusopt']['acct-timeout'] %}
acct-timeout={{authentication['radiusopt']['acct-timeout']}}
{% endif -%}
{% if authentication['radiusopt']['timeout'] %}
timeout={{authentication['radiusopt']['timeout']}}
{% endif -%}
{% if authentication['radiusopt']['max-try'] %}
max-try={{authentication['radiusopt']['max-try']}}
{% endif -%}
{% if authentication['radiusopt']['nas-id'] %}
nas-identifier={{authentication['radiusopt']['nas-id']}}
{% endif -%}
{% if authentication['radiusopt']['nas-ip'] %}
nas-ip-address={{authentication['radiusopt']['nas-ip']}}
{% endif -%}
{% if authentication['radiusopt']['dae-srv'] %}
dae-server={{authentication['radiusopt']['dae-srv']['ip-addr']}}:\
{{authentication['radiusopt']['dae-srv']['port']}},\
{{authentication['radiusopt']['dae-srv']['secret']}}
{% endif -%}
{% endif %}

[ppp]
verbose=1
check-ip=1
{% if mtu %}
mtu={{mtu}}
{% endif -%}
{% if ppp['mppe'] %}
mppe={{ppp['mppe']}}
{% endif -%}
{% if ppp['lcp-echo-interval'] %}
lcp-echo-interval={{ppp['lcp-echo-interval']}}
{% endif -%}
{% if ppp['lcp-echo-failure'] %}
lcp-echo-failure={{ppp['lcp-echo-failure']}}
{% endif -%}
{% if ppp['lcp-echo-timeout'] %}
lcp-echo-timeout={{ppp['lcp-echo-timeout']}}
{% endif %}

{% if authentication['radiusopt']['shaper'] %}
[shaper]
verbose=1
attr={{authentication['radiusopt']['shaper']['attr']}}
{% if authentication['radiusopt']['shaper']['vendor'] %}
vendor={{authentication['radiusopt']['shaper']['vendor']}}
{% endif -%}
{% endif %}

[cli]
tcp=127.0.0.1:2005
'''

### sstp chap secrets
chap_secrets_conf = '''
# username  server  password  acceptable local IP addresses   shaper
{% for user in authentication['local-users'] %}
{% if authentication['local-users'][user]['state'] == 'enabled' %}
{% if (authentication['local-users'][user]['upload']) and (authentication['local-users'][user]['download']) %}
{{user}}\t*\t{{authentication['local-users'][user]['passwd']}}\t{{authentication['local-users'][user]['ip']}}\t\
{{authentication['local-users'][user]['download']}}/{{authentication['local-users'][user]['upload']}}
{% else %}
{{user}}\t*\t{{authentication['local-users'][user]['passwd']}}\t{{authentication['local-users'][user]['ip']}}
{% endif %}
{% endif %}
{% endfor %}
'''
###
# inline helper functions
###
# depending on hw and threads, daemon needs a little to start
# if it takes longer than 100 * 0.5 secs, exception is being raised
# not sure if that's the best way to check it, but it worked so far quite well 
###
def chk_con():
  cnt = 0
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  while True:
    try:
      s.connect(("127.0.0.1", 2005))
      s.close()
      break
    except ConnectionRefusedError:
      time.sleep(0.5)
      cnt +=1
      if cnt == 100:
        raise("failed to start sstp server")
        break

### chap_secrets file if auth mode local
def write_chap_secrets(c):
  tmpl = jinja2.Template(chap_secrets_conf, trim_blocks=True)
  chap_secrets_txt = tmpl.render(c)
  old_umask = os.umask(0o077)
  open(chap_secrets,'w').write(chap_secrets_txt)
  os.umask(old_umask)
  sl.syslog(sl.LOG_NOTICE, chap_secrets + ' written')

def accel_cmd(cmd=''):
  if not cmd:
    return None
  try:
    ret = subprocess.check_output(['/usr/bin/accel-cmd','-p','2005',cmd]).decode().strip()
    return ret
  except:
    return 1

#### check ig local-ip is in client pool subnet


### 
# inline helper functions end
###

def get_config():
  c = Config()
  if not c.exists('service sstp-server'):
    return None

  c.set_level('service sstp-server')

  config_data = {
    'authentication'    : {
      'local-users'     : {
      },
      'mode'            : 'local',
      'auth_proto'      : [],
      'radius-srv'      : {},
      'radiusopt'       : {},
      'dae-srv'         : {}
    },
    'certs'           : {
      'ca'            : None,
      'server-key'    : None,
      'server-cert'   : None
    },
    'ip_pool'         : [],
    'gw'              : None,
    'dnsv4'           : {},
    'mtu'             : None,
    'ppp'             : {},
  }

  ### local auth
  if c.exists('authentication mode local'):
    if c.exists('authentication local-users'):
      for usr in c.list_nodes('authentication local-users username'):
        config_data['authentication']['local-users'].update(
          {
            usr : {
              'passwd'    : None,
              'state'     : 'enabled',
              'ip'        : '*',
              'upload'    : None,
              'download'  : None
            }
          }
        )
        if c.exists('authentication local-users username ' + usr + ' password'):
          config_data['authentication']['local-users'][usr]['passwd'] = c.return_value('authentication local-users username ' + usr + ' password')
        if c.exists('authentication local-users username ' + usr + ' disable'):
          config_data['authentication']['local-users'][usr]['state'] = 'disable'
        if c.exists('authentication local-users username ' + usr + ' static-ip'):
          config_data['authentication']['local-users'][usr]['ip'] = c.return_value('authentication local-users username ' + usr + ' static-ip')
        if c.exists('authentication local-users username ' + usr + ' rate-limit download'):
          config_data['authentication']['local-users'][usr]['download'] = c.return_value('authentication local-users username ' + usr + ' rate-limit download')
        if c.exists('authentication local-users username ' + usr + ' rate-limit upload'):
          config_data['authentication']['local-users'][usr]['upload'] = c.return_value('authentication local-users username ' + usr + ' rate-limit upload')

  if c.exists('authentication protocols'):
    auth_mods = {'pap' : 'pap','chap' : 'auth_chap_md5', 'mschap' : 'auth_mschap_v1', 'mschap-v2' : 'auth_mschap_v2'}
    for proto in c.return_values('authentication protocols'):
      config_data['authentication']['auth_proto'].append(auth_mods[proto])
  else:
    config_data['authentication']['auth_proto'] = ['auth_mschap_v2']

  #### RADIUS auth and settings
  if c.exists('authentication mode radius'):
    config_data['authentication']['mode'] = c.return_value('authentication mode')
    if c.exists('authentication radius-server'):
      for rsrv in c.list_nodes('authentication radius-server'):
        config_data['authentication']['radius-srv'][rsrv] = {}
        if c.exists('authentication radius-server ' + rsrv + ' secret'):
          config_data['authentication']['radius-srv'][rsrv]['secret'] = c.return_value('authentication radius-server ' + rsrv + ' secret')
        else:
          config_data['authentication']['radius-srv'][rsrv]['secret'] = None
        if c.exists('authentication radius-server ' + rsrv + ' fail-time'):
          config_data['authentication']['radius-srv'][rsrv]['fail-time'] = c.return_value('authentication radius-server ' + rsrv + ' fail-time')
        else:
          config_data['authentication']['radius-srv'][rsrv]['fail-time'] = 0
        if c.exists('authentication radius-server ' + rsrv + ' req-limit'):
          config_data['authentication']['radius-srv'][rsrv]['req-limit'] = c.return_value('authentication radius-server ' + rsrv + ' req-limit')
        else:
          config_data['authentication']['radius-srv'][rsrv]['req-limit'] = 0

    #### advanced radius-setting
    if c.exists('authentication radius-settings'):
      if c.exists('authentication radius-settings acct-timeout'):
        config_data['authentication']['radiusopt']['acct-timeout'] = c.return_value('authentication radius-settings acct-timeout')
      if c.exists('authentication radius-settings max-try'):
        config_data['authentication']['radiusopt']['max-try'] = c.return_value('authentication radius-settings max-try')
      if c.exists('authentication radius-settings timeout'):
        config_data['authentication']['radiusopt']['timeout'] = c.return_value('authentication radius-settings timeout')
      if c.exists('authentication radius-settings nas-identifier'):
        config_data['authentication']['radiusopt']['nas-id'] = c.return_value('authentication radius-settings nas-identifier')
      if c.exists('authentication radius-settings nas-ip-address'):
        config_data['authentication']['radiusopt']['nas-ip'] = c.return_value('authentication radius-settings nas-ip-address')
      if c.exists('authentication radius-settings dae-server'):
        config_data['authentication']['radiusopt'].update(
          {
            'dae-srv' : {
              'ip-addr' : c.return_value('authentication radius-settings dae-server ip-address'),
              'port'    : c.return_value('authentication radius-settings dae-server port'),
              'secret'  : str(c.return_value('authentication radius-settings dae-server secret'))
            }
          }
        )
      if c.exists('authentication radius-settings rate-limit enable'): 
        if not c.exists('authentication radius-settings rate-limit attribute'):
          config_data['authentication']['radiusopt']['shaper'] = { 'attr'  : 'Filter-Id' }
        else:
          config_data['authentication']['radiusopt']['shaper'] = {
            'attr'  : c.return_value('authentication radius-settings rate-limit attribute')
          }
        if c.exists('authentication radius-settings rate-limit vendor'):
          config_data['authentication']['radiusopt']['shaper']['vendor'] = c.return_value('authentication radius-settings rate-limit vendor')

  if c.exists('sstp-settings ssl-certs ca'):
    config_data['certs']['ca'] = c.return_value('sstp-settings ssl-certs ca')
  if c.exists('sstp-settings ssl-certs server-cert'):
    config_data['certs']['server-cert'] = c.return_value('sstp-settings ssl-certs server-cert')
  if c.exists('sstp-settings ssl-certs server-key'):
    config_data['certs']['server-key'] = c.return_value('sstp-settings ssl-certs server-key')

  if c.exists('network-settings client-ip-settings subnet'):
    config_data['ip_pool'] = c.return_values('network-settings client-ip-settings subnet')
  if c.exists('network-settings client-ip-settings gateway-address'):
    config_data['gw'] = c.return_value('network-settings client-ip-settings gateway-address')
  if c.exists('network-settings dns-server primary-dns'):
    config_data['dnsv4']['primary'] = c.return_value('network-settings dns-server primary-dns')
  if c.exists('network-settings dns-server secondary-dns'):
    config_data['dnsv4']['secondary'] = c.return_value('network-settings dns-server secondary-dns')
  if c.exists('network-settings mtu'):
    config_data['mtu'] = c.return_value('network-settings mtu')

  #### ppp
  if c.exists('ppp-settings mppe'):
    config_data['ppp']['mppe'] = c.return_value('ppp-settings mppe')
  if c.exists('ppp-settings lcp-echo-failure'):
    config_data['ppp']['lcp-echo-failure'] = c.return_value('ppp-settings lcp-echo-failure')
  if c.exists('ppp-settings lcp-echo-interval'):
    config_data['ppp']['lcp-echo-interval'] = c.return_value('ppp-settings lcp-echo-interval')
  if c.exists('ppp-settings lcp-echo-timeout'):
    config_data['ppp']['lcp-echo-timeout'] = c.return_value('ppp-settings lcp-echo-timeout')

  return config_data

def verify(c):
  if c == None:
    return None
  ### vertify auth settings
  if c['authentication']['mode'] == 'local':
    if not c['authentication']['local-users']:
      raise ConfigError('sstp-server authentication local-users required')

    for usr in c['authentication']['local-users']:
      if not c['authentication']['local-users'][usr]['passwd']:
        raise ConfigError('user ' + usr + ' requires a password')
      ### if up/download is set, check that both have a value
      if c['authentication']['local-users'][usr]['upload']:
        if not c['authentication']['local-users'][usr]['download']:
          raise ConfigError('user ' + usr + ' requires download speed value')
      if c['authentication']['local-users'][usr]['download']:
        if not c['authentication']['local-users'][usr]['upload']:
          raise ConfigError('user ' + usr + ' requires upload speed value')

  if not c['certs']['ca'] or not c['certs']['server-key'] or not c['certs']['server-cert']:
    raise ConfigError('service sstp-server sstp-settings ssl-certs needs the ssl certificates set up')
  else:
    ssl_path = ssl_cert_dir + '/'
    if not os.path.exists(ssl_path + c['certs']['ca']):
      raise ConfigError('CA {0} doesn\'t exist'.format(ssl_path + c['certs']['ca']))
    if not os.path.exists(ssl_path + c['certs']['server-cert']):
      raise ConfigError('SSL Cert {0} doesn\'t exist'.format(ssl_path + c['certs']['server-cert']))
    if not os.path.exists(ssl_path + c['certs']['server-cert']):
      raise ConfigError('SSL Key {0} doesn\'t exist'.format(ssl_path + c['certs']['server-key']))

  if c['authentication']['mode'] == 'radius':
    if len(c['authentication']['radius-srv']) == 0:
      raise ConfigError('service sstp-server authentication radius-server needs a value')
    for rsrv in c['authentication']['radius-srv']:
      if c['authentication']['radius-srv'][rsrv]['secret'] == None:
        raise ConfigError('service sstp-server authentication radius-server {0} secret requires a value'.format(rsrv))

  if c['authentication']['mode'] == 'local':
    if not c['ip_pool']:
      print ("WARNING: service sstp-server network-settings client-ip-settings subnet requires a value") 
    if not c['gw']:
      print ("WARNING: service sstp-server network-settings client-ip-settings gateway-address requires a value")
  
def generate(c):
  if c == None:
    return None
  
  ### accel-cmd reload doesn't work so any change results in a restart of the daemon
  try:
    if os.cpu_count() == 1:
      c['thread_cnt'] = 1
    else:
      c['thread_cnt'] = int(os.cpu_count()/2)
  except KeyError:
    if os.cpu_count() == 1:
      c['thread_cnt'] = 1
    else:
      c['thread_cnt'] = int(os.cpu_count()/2)

  tmpl = jinja2.Template(sstp_config, trim_blocks=True)
  config_text = tmpl.render(c)
  open(sstp_conf,'w').write(config_text)

  if c['authentication']['local-users']:
    write_chap_secrets(c)

  return c

def apply(c):
  if c == None:
    if os.path.exists(pidfile):
      accel_cmd('shutdown hard')
      if os.path.exists(pidfile):
        os.remove(pidfile)
    return None

  if not os.path.exists(pidfile):
    ret = subprocess.call(['/usr/sbin/accel-pppd','-c',sstp_conf,'-p',pidfile,'-d'])
    chk_con()
    if ret !=0 and os.path.exists(pidfile):
      os.remove(pidfile)
      raise ConfigError('accel-pppd failed to start')
  else:
    accel_cmd('restart')
    sl.syslog(sl.LOG_NOTICE, "reloading config via daemon restart")

if __name__ == '__main__':
  try:
    c = get_config()
    verify(c)
    generate(c)
    apply(c)
  except ConfigError as e:
    print(e)
    sys.exit(1)
