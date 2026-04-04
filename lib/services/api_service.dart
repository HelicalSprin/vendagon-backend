import 'dart:convert';
import 'package:http/http.dart' as http;

const String kBaseUrl = 'https://vendagon-backend.onrender.com';

class ApiService {
  final String baseUrl;
  final String token;

  ApiService({required this.token, String? baseUrl})
      : baseUrl = baseUrl ?? kBaseUrl;

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      };

  static Future<String> login(String username, String password) async {
    final resp = await http.post(
      Uri.parse('$kBaseUrl/auth/login'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'username': username, 'password': password}),
    );
    if (resp.statusCode == 200) {
      return jsonDecode(resp.body)['token'] as String;
    }
    throw Exception('Login failed: ${resp.body}');
  }

  Future<Map<String, dynamic>> getStatus() async {
    final resp = await http.get(
      Uri.parse('$baseUrl/machines/status?token=$token'),
      headers: _headers,
    );
    _checkResponse(resp);
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  Future<List<Map<String, dynamic>>> getProblemMachines() async {
    final resp = await http.get(
      Uri.parse('$baseUrl/machines/problems?token=$token'),
      headers: _headers,
    );
    _checkResponse(resp);
    return (jsonDecode(resp.body) as List).cast<Map<String, dynamic>>();
  }

  Future<List<Map<String, dynamic>>> getStockData() async {
    final resp = await http.get(
      Uri.parse('$baseUrl/machines/stock?token=$token'),
      headers: _headers,
    );
    _checkResponse(resp);
    return (jsonDecode(resp.body) as List).cast<Map<String, dynamic>>();
  }

  String get chartUrl => '$baseUrl/machines/stock/chart?token=$token';

  Future<List<int>> exportReport() async {
    final resp = await http.get(
      Uri.parse('$baseUrl/machines/report/export?token=$token'),
      headers: _headers,
    );
    _checkResponse(resp);
    return resp.bodyBytes.toList();
  }

  // ── Sales ─────────────────────────────────────────────────────────────────

  Future<Map<String, dynamic>> getSalesSummary({
    required int startDate,
    required int endDate,
    String? machineId,
    int page = 0,
    int limit = 100,
  }) async {
    final body = jsonEncode({
      'start_date': startDate,
      'end_date': endDate,
      if (machineId != null) 'machine_id': machineId,
      'page': page,
      'limit': limit,
    });
    final resp = await http.post(
      Uri.parse('$baseUrl/sales/summary?token=$token'),
      headers: _headers,
      body: body,
    );
    _checkResponse(resp);
    return jsonDecode(resp.body) as Map<String, dynamic>;
  }

  void _checkResponse(http.Response resp) {
    if (resp.statusCode == 401) throw Exception('Session expired. Please login again.');
    if (resp.statusCode != 200) throw Exception('Error ${resp.statusCode}: ${resp.body}');
  }
}
