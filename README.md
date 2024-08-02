# Translation Filesystem API Documentation

This document provides examples and explanations for using the Translation Filesystem API. The API allows you to add, remove, and list translations for the FUSE-based translation filesystem.

## Base URL

The API is accessible at `http://localhost:6000`. Adjust this if you've configured a different host or port.

## Endpoints

### 1. Add Translation

**Endpoint:** `/add_translation`
**Method:** POST
**Description:** Adds a new translation or updates an existing one.

#### Request Body

```json
{
  "original": "/path/to/original/file.txt",
  "translated": "/virtual/path/to/translated/file.txt"
}
```

#### Example using curl

```bash
curl -X POST http://localhost:6000/add_translation \
     -H "Content-Type: application/json" \
     -d '{"original": "/documents/english/hello.txt", "translated": "/documents/spanish/hola.txt"}'
```

#### Successful Response

```json
{
  "status": "success",
  "message": "Translation added successfully"
}
```

### 2. Remove Translation

**Endpoint:** `/remove_translation`
**Method:** POST
**Description:** Removes an existing translation.

#### Request Body

```json
{
  "original": "/path/to/original/file.txt"
}
```

#### Example using curl

```bash
curl -X POST http://localhost:6000/remove_translation \
     -H "Content-Type: application/json" \
     -d '{"original": "/documents/english/hello.txt"}'
```

#### Successful Response

```json
{
  "status": "success",
  "message": "Translation removed successfully"
}
```

### 3. List Translations

**Endpoint:** `/list_translations`
**Method:** GET
**Description:** Retrieves a list of all current translations.

#### Example using curl

```bash
curl http://localhost:6000/list_translations
```

#### Successful Response

```json
{
  "translations": [
    ["/documents/english/hello.txt", "/documents/spanish/hola.txt"],
    ["/documents/english/goodbye.txt", "/documents/spanish/adios.txt"]
  ]
}
```

## Error Handling

If an error occurs, the API will return a JSON response with a status code of 400 or 500, depending on the type of error. The response body will include an "error" field with a description of the error.

Example error response:

```json
{
  "status": "error",
  "message": "Missing 'original' or 'translated' in request"
}
```

## Usage Notes

1. Ensure that the FUSE filesystem is mounted and the API server is running before making requests.
2. The "original" path should be the actual path on your filesystem, while the "translated" path is the virtual path where you want the translated file to appear.
3. You can use the list_translations endpoint to verify that your translations have been added or removed successfully.
4. Remember that changes made through the API will be reflected in the mounted filesystem in real-time.

For any issues or feature requests, please contact the system administrator or file an issue in the project's repository.
