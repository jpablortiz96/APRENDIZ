"""Smoke de contrato sin modificar el escritorio: health, auth y validación."""
import argparse
import os
import httpx


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--url', required=True, help='URL pública del servicio APRENDIZ')
    p.add_argument('--token', default=os.environ.get('TEAM_TOKEN'), help='token del equipo; por defecto usa env TEAM_TOKEN')
    a=p.parse_args()
    if not a.token:
        p.error('Configura TEAM_TOKEN en el entorno (o --token).')
    root=a.url.rstrip('/')
    with httpx.Client(timeout=12) as c:
        r=c.get(root+'/health',headers={'Authorization':'Bearer '+a.token})
        assert r.status_code==200 and r.json().get('ok') is True, f'health falló: {r.status_code}: {r.text[:150]}'
        print('PASS /health 200 + ok:true')
        invalid={'tarea':'prueba','escritorio':{'url':'direccion-invalida','password':'no-usada'}}
        r=c.post(root+'/tasks', json=invalid)
        assert r.status_code==401, f'Sin auth debe dar 401; dio {r.status_code}: {r.text[:150]}'
        print('PASS /tasks sin Bearer -> 401')
        r=c.post(root+'/tasks', headers={'Authorization':'Bearer '+a.token}, json=invalid)
        assert r.status_code==400, f'URL inválida debe dar 400; dio {r.status_code}: {r.text[:150]}'
        print('PASS /tasks autenticado + URL inválida -> 400')
        r=c.get(root+'/tasks/no-existe', headers={'Authorization':'Bearer '+a.token})
        assert r.status_code==404, f'task inexistente debe dar 404; dio {r.status_code}: {r.text[:150]}'
        print('PASS /tasks/id inexistente -> 404')
    print('CONTRATO BÁSICO PASS. Faltan VNC, Claude y las 5 pruebas reales.')

if __name__=='__main__':
    main()
