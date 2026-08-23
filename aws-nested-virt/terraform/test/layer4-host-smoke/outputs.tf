output "instance_id" {
  description = "Test host instance ID — driver script polls SSM against this for the result marker."
  value       = aws_instance.test.id
}

output "public_ip" {
  description = "Public IP for SSH/curl debugging if needed."
  value       = aws_instance.test.public_ip
}

output "result_marker_path" {
  description = "Path on the host where userdata writes the pass/fail JSON."
  value       = "/var/log/layer4-result.json"
}
