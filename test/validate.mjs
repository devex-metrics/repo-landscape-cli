import { readFileSync } from 'node:fs';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const schema = JSON.parse(readFileSync(new URL('../schema/landscape-v1.schema.json', import.meta.url)));
const validate = addFormats(new Ajv2020({ allErrors: true })).compile(schema);
const result = JSON.parse(readFileSync(process.argv[2], 'utf8'));
if (!validate(result)) {
  console.error(validate.errors);
  process.exitCode = 1;
}
