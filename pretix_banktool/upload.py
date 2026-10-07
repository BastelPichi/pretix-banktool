import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import click
import requests
from fints.client import FinTS3PinTanClient, FinTSClientMode, NeedTANResponse
from fints.hhd.flicker import terminal_flicker_unix
from fints.utils import minimal_interactive_cli_bootstrap
from pretix_banktool import __version__
from pretix_banktool.config import get_endpoint, get_pin
from requests import RequestException


def ask_for_tan(client, response):
    click.echo("\nA TAN is required.")
    click.echo(response.challenge)

    if getattr(response, "challenge_hhduc", None):
        try:
            terminal_flicker_unix(response.challenge_hhduc)
        except KeyboardInterrupt:
            pass

    if response.decoupled:
        input("Confirm the request in your banking app, then press Enter...")
        tan = ""
    else:
        tan = input("Please enter TAN: ").strip()

    return client.send_tan(response, tan)


def resolve_tan(client, response):
    while isinstance(response, NeedTANResponse):
        response = ask_for_tan(client, response)
    return response


def get_system_id(config):
    state_file = config['fints'].get('state_file')
    if state_file and Path(state_file).exists():
        system_id = Path(state_file).read_text().strip()
        if system_id:
            click.echo(f"Restoring saved System ID: {system_id}")
            return system_id
    return None


def save_system_id(config, client):
    state_file = config['fints'].get('state_file')
    if state_file and client.system_id and client.system_id != "0":
        Path(state_file).write_text(client.system_id)
        click.echo(f"Saved System ID to {state_file}")


def upload_transactions(config, days=30, pending=False, bank_ids=False, ignore=None):
    ignore = ignore or []
    ignore_patterns = []
    for i in ignore:
        try:
            ignore_patterns.append(re.compile(i))
        except re.error as e:
            click.echo(click.style('Not a valid regular expression: %s' % i, fg='red'))
            click.echo(click.style('"%s" at position %d' % (e.msg, e.pos), fg='red'))

    click.echo('Creating FinTS client...')

    f = FinTS3PinTanClient(
        config['fints']['blz'],
        config['fints']['username'],
        get_pin(config),
        config['fints']['endpoint'],
        mode=FinTSClientMode.INTERACTIVE,
        product_id='459BE10AAEE93C6AA90BE6FE3',
        product_version=__version__,
        system_id=get_system_id(config)
    )

    # Bootstraps mechanisms & setup
    minimal_interactive_cli_bootstrap(f)

    with f:
        if isinstance(f.init_tan_response, NeedTANResponse):
            f.init_tan_response = resolve_tan(f, f.init_tan_response)

        click.echo('Fetching SEPA account list...')
        accounts = resolve_tan(f, f.get_sepa_accounts())

        click.echo('Looking for correct SEPA account...')
        accounts_matching = [a for a in accounts if a.iban == config['fints']['iban']]
        if not accounts_matching:
            click.echo(click.style('The specified SEPA account %s could not be found.' % config['fints']['iban'], fg='red'))
            click.echo('Only the following SEPA accounts were detected:')
            click.echo(', '.join([a.iban for a in accounts]))
            sys.exit(1)
        elif len(accounts_matching) > 1:
            click.echo(click.style('Multiple SEPA accounts match the given IBAN.', fg='red'))
            click.echo('Only the following SEPA accounts were detected:')
            click.echo(', '.join([a.iban for a in accounts]))
            sys.exit(1)

        account = accounts_matching[0]
        click.echo(click.style('Found matching SEPA account.', fg='green'))

        click.echo('Fetching statement of the last %d days...' % days)
        statement = resolve_tan(
            f,
            f.get_transactions(
                account,
                date.today() - timedelta(days=days),
                date.today(),
                include_pending=pending
            )
        )

        if statement:
            click.echo(click.style('Found %d transactions.' % len(statement), fg='green'))
            click.echo('Parsing...')

            transactions = []
            ignored = 0
            for transaction in statement:
                reference = ' '.join(
                    transaction.data.get(t)
                    for t in (
                        'posting_text', 'purpose', 'bank_reference', 'customer_reference'
                    )
                    if transaction.data.get(t)
                )
                payer = {
                    'name': transaction.data.get('applicant_name', ''),
                    'iban': transaction.data.get('applicant_iban', ''),
                }
                eref = transaction.data.get('end_to_end_reference', '')

                ignore_tx = False
                for i in ignore_patterns:
                    if i.search(reference):
                        ignore_tx = True
                        ignored += 1
                        break

                if not ignore_tx:
                    tx = {
                        'amount': str(transaction.data['amount'].amount),
                        'reference': reference + (' EREF: {}'.format(eref) if eref else ''),
                        'payer': (payer.get('name') or ''),
                        'iban': (payer.get('iban') or ''),
                        'date': transaction.data['date'].isoformat(),
                    }
                    if bank_ids and transaction.data.get('bank_reference'):
                        tx['external_id'] = transaction.data.get('bank_reference')

                    transactions.append(tx)

            if ignored > 0:
                click.echo(click.style('Ignored %d transactions.' % ignored, fg='blue'))
            payload = {
                'event': None,
                'transactions': transactions
            }

            click.echo('Uploading...')
            try:
                r = requests.post(
                    get_endpoint(config),
                    headers={
                        'Authorization': 'Token {}'.format(config['pretix']['key']),
                        'User-Agent': f'pretix-banktool/{__version__}',
                    },
                    json=payload,
                    verify=not config.getboolean('pretix', 'insecure', fallback=False)
                )
                if r.status_code == 201:
                    click.echo(click.style('Job uploaded.', fg='green'))
                else:
                    click.echo(click.style('Invalid response code: %d' % r.status_code, fg='red'))
                    click.echo(r.text)
                    sys.exit(2)
            except (RequestException, OSError) as e:
                click.echo(click.style('Connection error: %s' % str(e), fg='red'))
                sys.exit(2)
            except ValueError as e:
                click.echo(click.style('Could not read response: %s' % str(e), fg='red'))
                sys.exit(2)
        else:
            click.echo('No recent transaction found.')

    save_system_id(config, f)
