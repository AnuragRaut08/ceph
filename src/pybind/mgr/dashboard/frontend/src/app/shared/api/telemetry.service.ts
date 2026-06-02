import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

@Injectable({
  providedIn: 'root'
})
export class TelemetryService {
  private url = 'api/telemetry';

  constructor(private http: HttpClient) {}

  getReport() {
    return this.http.get(`${this.url}/report`);
  }

  enable(enable: boolean = true) {
    const body = { enable: enable };
    if (enable) {
      body['license_name'] = 'sharing-1-0';
    }
    return this.http.put(`${this.url}`, body);
  }

  /**
   * Track a page visit or user interaction event
   * @param eventName - The name of the event (e.g., 'rgw.bucket_list', 'pools.list')
   * @returns Observable with the response containing success status and counts
   */
  trackEvent(eventName: string): Observable<any> {
    return this.http.post(`${this.url}/event`, { event_name: eventName });
  }
}
