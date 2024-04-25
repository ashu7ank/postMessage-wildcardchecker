#Takes CSV I/P of organisation names only and checks in all repos.

import asyncio
import csv
import random
import re
import ssl
import sys
from collections import defaultdict
from datetime import datetime

import pytz
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import (ClientError, ClientResponseError,
                                       ServerDisconnectedError)


class GithubAPIClient:
    def __init__(self, base_url, access_token):
        self.base_url = base_url
        self.access_token = access_token
        self.session = None
        self.rate_limit_remaining = None
        self.rate_limit_reset_time = None
        self.rate_limit_reached = False
        self.retry_count = defaultdict(int)  # Track retry counts for each repository
        self.last_403_time = defaultdict(float)  # Track the last time a 403 error occurred for each repository
    async def __aenter__(self):
        self.session = ClientSession(headers={'Authorization': f'token {self.access_token}'})
        return self
    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.session.close()
    async def fetch_repositories(self, org_name, page=1, per_page=100):
        if self.rate_limit_reached:
            # Handle rate limit reached scenario
            return None
        url = f'{self.base_url}/orgs/{org_name}/repos?page={page}&per_page={per_page}'
        async with self.session.get(url, timeout=ClientTimeout(total=60)) as response:
            await self.update_rate_limits(response)
            if response.status != 200:
                print(f'Error: {response.status} - Unable to fetch repositories for {org_name}')
                return None
            data = await response.json()
            return data
    async def fetch_file(self, file_url):
        try:
            timeout = ClientTimeout(total=60)
            async with self.session.get(file_url, timeout=timeout) as response:
                await self.update_rate_limits(response)
                if response.status == 200:
                    file_contents = await response.text(errors='replace')
                    return file_contents
        except (ClientError, ssl.SSLError, asyncio.TimeoutError) as e:
            print(f'Error fetching file: {e}')
            raise
    async def process_organization(self, org_name, csv_writer, semaphore):
        if self.rate_limit_reached:
            return
        repositories = await self.fetch_repositories(org_name)
        if repositories is None:
            return
        tasks = []
        for repo in repositories:
            tasks.append(self.process_files(org_name, repo['name'], csv_writer, semaphore))
            await asyncio.sleep(random.uniform(1, 3))  # Introduce random jitter between requests
        await asyncio.gather(*tasks)
    async def process_files(self, org_name, repo_name, csv_writer, semaphore):
        url = f'{self.base_url}/repos/{org_name}/{repo_name}/contents'
        try:
            async with semaphore:
                async with self.session.get(url, timeout=ClientTimeout(total=60)) as response:
                    await self.update_rate_limits(response)
                    if response.status != 200:
                        if response.status == 404:
                            print(f'Error: {response.status} - Unable to fetch repository contents for {org_name}/{repo_name}')
                        elif response.status == 403:
                            print(f'Error: {response.status} - Forbidden: Unable to access {org_name}/{repo_name}')
                            await self.handle_403_error(org_name, repo_name, csv_writer, semaphore)
                            return
                        else:
                            print(f'Error: {response.status} - Unable to fetch repository contents for {org_name}/{repo_name}')
                        return
                    contents = await response.json()
                    tasks = []
                    for item in contents:
                        if item['type'] == 'file' and item['name'].endswith(('.html', '.js', '.ts','.jsx','.jsx')):
                            tasks.append(self.process_file(item['download_url'], org_name, repo_name, csv_writer))
                        elif item['type'] == 'dir':
                            tasks.append(self.process_directory(item['name'], f'{url}/{item["name"]}', org_name, repo_name, csv_writer))
                    if tasks:
                        await asyncio.gather(*tasks)
        except (ClientError, asyncio.TimeoutError, ServerDisconnectedError) as e:
            print(f'Error processing files for {org_name}/{repo_name}: {e}')

    async def handle_403_error(self, org_name, repo_name, csv_writer, semaphore):
        repo_key = f"{org_name}/{repo_name}"
        self.retry_count[repo_key] += 1
        if self.retry_count[repo_key] >= 5:
            print(f'Maximum retries reached for {repo_key}. Skipping this repository.')
            return
        print(f'Skipping repository {repo_key} due to 403 Forbidden error.')
        return

    async def process_directory(self, directory_name, directory_url, org_name, repo_name, csv_writer):
        try:
            async with self.session.get(directory_url, timeout=ClientTimeout(total=60)) as response:
                await self.update_rate_limits(response)
                if response.status != 200:
                    if response.status == 403:
                        print(f'Error processing directory {directory_name}: {response.status}, message={response.reason}, url={directory_url}')
                        await self.handle_403_error(org_name, repo_name, csv_writer, None)
                    else:
                        print(f'Error processing directory {directory_name}: {response.status} - {response.reason}')
                    return
                contents = await response.json()
                tasks = []
                for item in contents:
                    if item['type'] == 'file' and item['name'].endswith(('.html', '.js', '.ts','.jsx')):
                        tasks.append(self.process_file(item['download_url'], org_name, repo_name, csv_writer))
                    elif item['type'] == 'dir':
                        tasks.append(self.process_directory(item['name'], f'{directory_url}/{item["name"]}', org_name, repo_name, csv_writer))
                if tasks:
                    await asyncio.gather(*tasks)
        except ClientResponseError as e:
            if e.status == 403:
                print(f'Error processing directory {directory_name}: {e.status}, message={e.message}, url={e.request_info.url}')
                await self.handle_403_error(org_name, repo_name, csv_writer, None)
            else:
                print(f'Error processing directory {directory_name}: {e}')
        except (asyncio.TimeoutError, ServerDisconnectedError) as e:
            print(f'Error processing directory {directory_name}: {e}')
            await asyncio.sleep(2)
            await self.process_directory(directory_name, directory_url, org_name, repo_name, csv_writer)
    async def process_file(self, file_url, org_name, repo_name, csv_writer):
        retry_count = 0
        while retry_count < 5:
            try:
                file_contents = await self.fetch_file(file_url)
                if file_contents is not None:
                    pattern = re.compile(r'postMessage\s*\(\s*(?:[^()]*\([^()]*\))*[^()]*\s*,\s*[\'"]\*\s*[\'"]\s*\)')
                    matches = pattern.findall(file_contents)
                    if matches:
                        for match in matches:
                            csv_writer.writerow([org_name, repo_name, file_url, '-', match.strip()])
                    return
            except (ClientError, ssl.SSLError, asyncio.TimeoutError) as e:
                print(f'Error processing file: {e}')
                retry_count += 1
                await asyncio.sleep(2 ** retry_count + random.uniform(0, 1))
    async def update_rate_limits(self, response):
        self.rate_limit_remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
        reset_timestamp = int(response.headers.get('X-RateLimit-Reset', 0))
        reset_utc = datetime.utcfromtimestamp(reset_timestamp)
        reset_local = pytz.utc.localize(reset_utc).astimezone(pytz.timezone('Asia/Kolkata'))  # Adjust 'Asia/Kolkata' to your time zone
        self.rate_limit_reset_time = reset_local.strftime('%Y-%m-%d %H:%M:%S')
        if self.rate_limit_remaining <= 0:
            print(f'Rate limit exceeded. Waiting for at least one minute before retrying...')
            await asyncio.sleep(60)  # Wait for at least one minute before retrying
            print(f'Retrying after waiting for one minute...')
        else:
            print(f'Rate limit remaining: {self.rate_limit_remaining}, Retry after {self.rate_limit_reset_time}')
async def main():
    if len(sys.argv) < 3:
        print('Usage: python3 script.py  <csv_file_path> <github_token>')
        return
    csv_file_path = sys.argv[1]
    access_token = sys.argv[2]
    base_url = 'https://github.enterprise-name.com/api/v3'
    csv_filename = 'results.csv'
    with open(csv_file_path, 'r') as csvfile, open(csv_filename, 'w', newline='') as output_file:
        csv_reader = csv.reader(csvfile)
        csv_writer = csv.writer(output_file)
        csv_writer.writerow(['Organization', 'Repository', 'File Name', 'Line Number', 'Code Line'])
        next(csv_reader)  # Skip the header row
        for row in csv_reader:
            org_name = row[0].strip()
            semaphore = asyncio.Semaphore(5)  # Limit the number of concurrent tasks
            async with GithubAPIClient(base_url, access_token) as client:
                await client.process_organization(org_name, csv_writer, semaphore)
                print(f'Processing completed for {org_name}.')
    print(f'All organizations processed. Results saved in {csv_filename}')
if __name__ == '__main__':
    asyncio.run(main())
